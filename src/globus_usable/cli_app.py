from __future__ import annotations

import re
import shutil
import stat as statmod
import sys
from datetime import datetime
from pathlib import Path

import questionary
import rich_click as click

from .config import (
    ConfigError,
    SYNC_LEVELS,
    default_config_path,
    load_config,
    render_init_config_toml,
    resolve_remote_endpoint_id,
)
from .errors import GlobusUsableError
from .globus_cli import parse_json
from .log import configure_logger
from .metrics import clamp_percent
from .progress import TaskSpec, poll_tasks
from .transfer import (
    build_transfer_requests,
    normalize_remote_path,
    parse_path,
    run_globus,
    submit_transfer,
)
from .units import GIB, MIB

click.rich_click.TEXT_MARKUP = "rich"


def _json_data(output: str, context: str) -> list[dict]:
    try:
        payload = parse_json(output, context, empty="empty")
    except GlobusUsableError as exc:
        raise click.ClickException(str(exc)) from exc
    data = payload.get("DATA", [])
    if data is None:
        return []
    if not isinstance(data, list):
        raise click.ClickException(f"Unexpected JSON from Globus CLI while {context}")
    return data


def _local_ls(path: Path, *, long: bool, show_all: bool) -> str:
    target = path.resolve()
    if not target.exists():
        raise click.ClickException(f"No such path: {path}")
    if target.is_file():
        entries = [target]
    else:
        entries = sorted(target.iterdir(), key=lambda p: p.name)
    if not show_all:
        entries = [p for p in entries if not p.name.startswith(".")]

    lines: list[str] = []
    for p in entries:
        name = p.name + ("/" if p.is_dir() else "")
        if not long:
            lines.append(name)
            continue
        st = p.lstat()
        mode = statmod.filemode(st.st_mode)
        size = st.st_size
        mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        lines.append(f"{mode} {size:>10} {mtime} {name}")
    return "\n".join(lines)


@click.group()
def cli() -> None:
    """Globus CLI wrapper with rsync-like semantics."""
    configure_logger()


_REMOTE_NAME_ALLOWED_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug_remote_name(value: str) -> str:
    slug = _REMOTE_NAME_ALLOWED_RE.sub("-", value.strip().lower()).strip("-")
    return slug or "remote"


@cli.command()
@click.argument("src")
@click.argument("dst")
@click.option("-r", "--recursive", is_flag=True, help="Recursively copy directories")
@click.option(
    "-s",
    "--sync-level",
    type=click.Choice(SYNC_LEVELS),
    default="mtime",
    show_default=True,
    help="Transfer criterion",
)
@click.option(
    "-L",
    "--dereference/--no-dereference",
    default=True,
    show_default=True,
    help="Follow symlinks (default)",
)
@click.option("--no-links", is_flag=True, help="Skip symlinks entirely")
@click.option(
    "--continue-on-error",
    is_flag=True,
    help="Continue transferring when errors occur (default: fail fast)",
)
@click.option("--quiet", is_flag=True, help="Suppress progress output, show only summary/errors")
@click.option("--json", "json_mode", is_flag=True, help="Output NDJSON events for scripting")
def cp(
    src: str,
    dst: str,
    recursive: bool,
    sync_level: str,
    dereference: bool,
    no_links: bool,
    continue_on_error: bool,
    quiet: bool,
    json_mode: bool,
) -> None:
    """Copy files between local and remote endpoints (including remote-to-remote)."""
    if quiet and json_mode:
        raise click.ClickException("Use only one of --quiet or --json.")

    try:
        cfg = load_config()
        ctx = click.get_current_context()
        if ctx.get_parameter_source("sync_level") == click.core.ParameterSource.DEFAULT:
            sync_level = cfg.defaults.sync_level

        requests = build_transfer_requests(
            run_globus,
            cfg,
            src=src,
            dst=dst,
            recursive=recursive,
            sync_level=sync_level,
            dereference=dereference,
            no_links=no_links,
        )

        task_specs: list[TaskSpec] = []
        is_glob_loop = len(requests) > 1
        for req in requests:
            try:
                task_id = submit_transfer(run_globus, req)
            except GlobusUsableError as exc:
                if not is_glob_loop:
                    raise
                click.echo(
                    f"warning: failed to submit transfer for {req.label}: {exc}",
                    err=True,
                )
                continue
            task_specs.append(
                TaskSpec(task_id=task_id, label=req.label, src=req.src_path, dst=req.dst_path)
            )

        if not task_specs:
            raise GlobusUsableError("Failed to submit any transfer tasks.")

        if quiet:
            mode = "quiet"
        elif json_mode:
            mode = "json"
        else:
            mode = "interactive"

        try:
            result = poll_tasks(
                run_globus,
                task_specs,
                mode=mode,
                poll_min=cfg.defaults.poll_interval_min,
                poll_max=cfg.defaults.poll_interval_max,
                abort_on_error=not continue_on_error,
            )
        except KeyboardInterrupt:
            if not task_specs:
                raise

            keep_running = True
            if not json_mode and sys.stdin is not None and sys.stdin.isatty():
                try:
                    keep_running = click.confirm(
                        "Keep transfer task(s) running in the background?",
                        default=True,
                    )
                except KeyboardInterrupt:
                    keep_running = True

            task_ids = ", ".join(t.task_id for t in task_specs)
            if keep_running:
                click.echo(f"\nLeaving transfer task(s) running: {task_ids}", err=json_mode)
                return

            cancel_failures: list[str] = []
            for t in task_specs:
                try:
                    run_globus("task", "cancel", t.task_id)
                except GlobusUsableError as exc:
                    text = str(exc).lower()
                    if (
                        "not active" in text
                        or "already completed" in text
                        or "already inactive" in text
                        or "not cancellable" in text
                        or "cannot be cancelled" in text
                        or "cannot be canceled" in text
                    ):
                        continue
                    cancel_failures.append(f"{t.task_id}: {exc}")

            if cancel_failures:
                details = "; ".join(cancel_failures[:3])
                if len(cancel_failures) > 3:
                    details = f"{details}; ..."
                raise click.ClickException(f"Failed to cancel transfer task(s): {details}")

            click.echo(f"\nCancelled transfer task(s): {task_ids}", err=json_mode)
            return

        if quiet:
            if result.errors:
                messages = "; ".join(sorted(result.errors.values()))
                raise GlobusUsableError(messages)
            total_files = sum((t.get("files") or 0) for t in result.task_data.values())
            total_bytes = sum((t.get("bytes_transferred") or 0) for t in result.task_data.values())
            gb = total_bytes / GIB
            sys.stdout.write(f"Transferred {total_files} files ({gb:.2f} GB)\n")
            sys.stdout.flush()
            return

        if result.errors and continue_on_error:
            messages = "; ".join(sorted(result.errors.values()))
            raise GlobusUsableError(messages)
    except (ConfigError, GlobusUsableError) as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command()
@click.argument("path", required=False, default=".")
@click.option("-l", "--long", is_flag=True, help="Long format with details")
@click.option("-a", "--all", "show_all", is_flag=True, help="Show hidden files")
def ls(path: str, long: bool, show_all: bool) -> None:
    """List files on local filesystem or remote endpoint."""
    try:
        cfg = load_config()
        parsed = parse_path(path, cfg)
        if not parsed.is_remote:
            click.echo(_local_ls(Path(path), long=long, show_all=show_all))
            return
        remote_ep = resolve_remote_endpoint_id(cfg, parsed.remote or cfg.defaults.default_remote)
        args = ["ls"]
        if long:
            args.append("-l")
        if show_all:
            args.append("-a")
        remote_path = normalize_remote_path(parsed.path or "")
        click.echo(run_globus(*args, f"{remote_ep}:{remote_path}"))
    except (ConfigError, GlobusUsableError) as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command()
@click.argument("src")
@click.argument("dst")
def mv(src: str, dst: str) -> None:
    """Rename/move files within same endpoint."""
    if ":" not in src and ":" not in dst:
        try:
            shutil.move(src, dst)
        except OSError as exc:
            raise click.ClickException(str(exc)) from exc
        return

    try:
        cfg = load_config()
        ps = parse_path(src, cfg)
        pd = parse_path(dst, cfg)
    except (ConfigError, GlobusUsableError) as exc:
        raise click.ClickException(str(exc)) from exc

    if ps.is_remote != pd.is_remote:
        raise click.ClickException("Cross-endpoint moves are not supported; use cp + delete.")

    if not ps.is_remote:
        try:
            shutil.move(src, dst)
        except OSError as exc:
            raise click.ClickException(str(exc)) from exc
        return

    if ps.remote != pd.remote:
        raise click.ClickException("Cross-remote moves are not supported; use cp + delete.")

    try:
        remote_ep = resolve_remote_endpoint_id(cfg, ps.remote or cfg.defaults.default_remote)
        src_path = normalize_remote_path(ps.path or "")
        dst_path = normalize_remote_path(pd.path or "")
        click.echo(run_globus("rename", remote_ep, src_path, dst_path))
    except (ConfigError, GlobusUsableError) as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command()
@click.option("-l", "--live", is_flag=True, help="Live progress bars with real-time updates")
def status(live: bool) -> None:
    """Show transfer status."""
    cfg = load_config()
    out = run_globus("task", "list", "--filter-status", "ACTIVE", "--format", "json")
    data = _json_data(out, "listing active tasks")
    if not data:
        out = run_globus("task", "list", "--limit", "1", "--format", "json")
        data = _json_data(out, "listing recent tasks")
    if not data:
        click.echo("No transfers found.")
        return

    if live:
        tasks = [TaskSpec(task_id=t["task_id"], label=(t.get("label") or t["task_id"])) for t in data]
        try:
            poll_tasks(
                run_globus,
                tasks,
                mode="interactive",
                poll_min=cfg.defaults.poll_interval_min,
                poll_max=cfg.defaults.poll_interval_max,
                abort_on_error=False,
            )
        except KeyboardInterrupt:
            click.echo("\nStopped monitoring.")
        return

    click.echo(f"Found {len(data)} transfer(s)")
    for i, t in enumerate(data):
        if i > 0:
            click.echo()
        done = t.get("subtasks_succeeded") or 0
        total = t.get("subtasks_total") or 0
        pct = clamp_percent(int(done), int(total))
        gb = (t.get("bytes_transferred") or 0) / GIB
        speed = (t.get("effective_bytes_per_second") or 0) / MIB
        label = t.get("label") or t["task_id"]
        click.echo(f"[{i + 1}/{len(data)}] {label}")
        click.echo(f"  Status: {t.get('status')} ({pct:.1f}% - {done}/{total or '?'})")
        click.echo(f"  Source: {t.get('source_endpoint_display_name')}")
        click.echo(f"  Dest:   {t.get('destination_endpoint_display_name')}")
        click.echo(f"  Size:   {gb:.2f} GB @ {speed:.1f} MB/s")


@cli.command()
@click.argument("task_id", required=False)
@click.option("--all", "cancel_all", is_flag=True, help="Cancel all active tasks")
def cancel(task_id: str | None, cancel_all: bool) -> None:
    """Cancel transfers."""
    if cancel_all:
        out = run_globus("task", "list", "--filter-status", "ACTIVE", "--format", "json")
        data = _json_data(out, "listing active tasks")
        if not data:
            click.echo("No active tasks found.")
            return
        for t in data:
            run_globus("task", "cancel", t["task_id"])
        click.echo(f"Cancelled {len(data)} task(s).")
        return

    if task_id:
        run_globus("task", "cancel", task_id)
        click.echo(f"Cancelled task {task_id}.")
        return

    out = run_globus("task", "list", "--limit", "1", "--filter-status", "ACTIVE", "--format", "json")
    data = _json_data(out, "listing active tasks")
    if not data:
        out = run_globus("task", "list", "--limit", "1", "--format", "json")
        data = _json_data(out, "listing recent tasks")
    if not data:
        click.echo("No tasks found.")
        return
    tid = data[0]["task_id"]
    run_globus("task", "cancel", tid)
    click.echo(f"Cancelled task {tid}.")


@cli.group()
def config() -> None:
    """Manage the globus-usable config file."""


@config.command("list")
@click.option(
    "--path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Config file path (default: ~/.config/globus-usable/remotes.toml)",
)
def config_list(path: Path | None) -> None:
    """Show the loaded config and its location."""
    config_path = path or default_config_path()
    try:
        cfg = load_config(config_path)
    except (ConfigError, GlobusUsableError) as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Config path: {config_path}")
    click.echo(f"Local endpoint_id: {cfg.local_endpoint_id or '(missing)'}")
    click.echo("Remotes:")
    if cfg.remotes:
        for name, endpoint_id in sorted(cfg.remotes.items(), key=lambda kv: kv[0]):
            suffix = " (default)" if name == cfg.defaults.default_remote else ""
            click.echo(f"  - {name} = {endpoint_id}{suffix}")
    else:
        click.echo("  (none)")
    click.echo("Defaults:")
    click.echo(f"  default_remote = {cfg.defaults.default_remote}")
    click.echo(f"  sync_level = {cfg.defaults.sync_level}")
    click.echo(f"  poll_interval_min = {cfg.defaults.poll_interval_min:g}")
    click.echo(f"  poll_interval_max = {cfg.defaults.poll_interval_max:g}")


@config.command("init")
@click.option(
    "--path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Config file path (default: ~/.config/globus-usable/remotes.toml)",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite an existing config file",
)
@click.option(
    "--autopopulate-linked-collections",
    is_flag=True,
    help="Populate [remotes.*] from linked (mapped) collections you can access",
)
def config_init(path: Path | None, force: bool, autopopulate_linked_collections: bool) -> None:
    """Create a starter config file."""
    ctx = click.get_current_context()
    if ctx.get_parameter_source("autopopulate_linked_collections") == click.core.ParameterSource.DEFAULT:
        if sys.stdin is not None and sys.stdin.isatty():
            autopopulate_linked_collections = click.confirm(
                "Autopopulate config with UUIDs of linked collections you can access (writes them under [remotes.*])?",
                default=True,
            )
        else:
            autopopulate_linked_collections = False

    config_path = path or default_config_path()
    if config_path.exists() and not force:
        raise click.ClickException(f"Config already exists at {config_path}; use --force to overwrite.")

    local_endpoint_id: str | None = None
    local_endpoint_error: str | None = None
    try:
        local_endpoint_id = run_globus("endpoint", "local-id").strip() or None
    except GlobusUsableError as exc:
        local_endpoint_error = str(exc)

    remotes: dict[str, str] = {}
    default_remote: str | None = None

    if autopopulate_linked_collections:
        try:
            collections = _discover_accessible_linked_collections()
            if sys.stdin is not None and sys.stdin.isatty():
                collections = _select_linked_collections_interactive(collections)
        except GlobusUsableError as exc:
            raise click.ClickException(str(exc)) from exc

        used_names: set[str] = set()
        for display_name, collection_id in collections:
            base = _slug_remote_name(display_name)
            name = base
            suffix = 2
            while name in used_names:
                name = f"{base}-{suffix}"
                suffix += 1
            used_names.add(name)
            remotes[name] = collection_id

        if remotes:
            default_remote = sorted(remotes.keys())[0]

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        render_init_config_toml(
            remotes=remotes,
            default_remote=default_remote,
            local_endpoint_id=local_endpoint_id,
        ),
        encoding="utf-8",
    )
    click.echo(f"Wrote config to {config_path}")

    if local_endpoint_id:
        click.echo(f"Local endpoint_id = {local_endpoint_id}")
    else:
        detail = f" ({local_endpoint_error})" if local_endpoint_error else ""
        click.secho(
            "Warning: local endpoint_id not detected; config contains a placeholder. "
            f"Run `globus endpoint local-id` and update [local].endpoint_id{detail}.",
            fg="yellow",
            err=True,
        )

    if autopopulate_linked_collections and not remotes:
        click.echo("No linked collections found (or none accessible); wrote a starter config without [remotes.*].")
        return

    if remotes:
        click.echo("Populated remotes:")
        for name, collection_id in sorted(remotes.items(), key=lambda kv: kv[0]):
            click.echo(f"  - {name} = {collection_id}")

        example_remote = sorted(remotes.keys())[0]
        click.echo("Usage examples:")
        click.echo(f"  globus-usable ls {example_remote}:/")
        click.echo(f"  globus-usable cp ./local-file.txt {example_remote}:/path/")
        click.echo("Custom config location:")
        click.echo("  globus-usable config init --path /path/to/remotes.toml")


def _discover_accessible_linked_collections(
    *,
    scope: str = "my-endpoints",
    fulltext: str | None = None,
    limit: int = 1000,
) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    seen_ids: set[str] = set()

    def is_linked_collection_entity_type(value: object) -> bool:
        if not isinstance(value, str):
            return False
        normalized = value.strip().lower()
        return (
            normalized.endswith("_mapped_collection")
            or normalized.endswith("_guest_collection")
            or normalized == "gcsv4_share"
        )

    args: list[str] = [
        "endpoint",
        "search",
        "--filter-scope",
        scope,
        "--limit",
        str(limit),
        "--format",
        "json",
    ]
    if fulltext is not None and fulltext.strip():
        args.append(fulltext)
    out = run_globus(*args)
    context = f"searching {scope} for linked collections"
    if fulltext is not None and fulltext.strip():
        context = f"{context} matching {fulltext!r}"
    data = _json_data(out, context)
    for entry in data:
        if not isinstance(entry, dict):
            continue
        entity_type = entry.get("entity_type")
        if not is_linked_collection_entity_type(entity_type):
            continue
        collection_id = entry.get("id")
        if not isinstance(collection_id, str) or not collection_id.strip():
            continue
        if collection_id in seen_ids:
            continue
        seen_ids.add(collection_id)
        display = entry.get("display_name")
        display_name = display.strip() if isinstance(display, str) and display.strip() else collection_id
        items.append((display_name, collection_id))

    items.sort(key=lambda x: x[0].lower())
    return items


def _select_linked_collections_interactive(
    initial: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    items_by_id: dict[str, tuple[str, str]] = {
        collection_id: (display_name, collection_id) for display_name, collection_id in initial
    }
    selected_ids: set[str] = set(items_by_id.keys())

    while True:
        choices: list[questionary.Choice] = []
        for display_name, collection_id in sorted(items_by_id.values(), key=lambda x: x[0].lower()):
            title = f"{display_name} ({collection_id})" if display_name != collection_id else collection_id
            choices.append(
                questionary.Choice(
                    title=title,
                    value=collection_id,
                    checked=(collection_id in selected_ids),
                )
            )

        picked = questionary.checkbox(
            "Select collections to add to config ([space] toggles, [enter] confirms)",
            choices=choices,
        ).ask()
        if picked is None:
            return []
        selected_ids = {v for v in picked if isinstance(v, str)}

        want_search = questionary.confirm(
            "Search for more collections/endpoints to add?",
            default=False,
        ).ask()
        if not want_search:
            break

        term = questionary.text("Search term").ask()
        if term is None or not term.strip():
            continue

        discovered = _discover_accessible_linked_collections(
            scope="all",
            fulltext=term,
            limit=200,
        )
        additional = [
            (display_name, collection_id)
            for display_name, collection_id in discovered
            if collection_id not in items_by_id
        ]
        if not additional:
            click.echo("No additional linked collections found for that search term.")
            continue

        additional_by_id: dict[str, tuple[str, str]] = {cid: (name, cid) for name, cid in additional}
        additional_choices: list[questionary.Choice] = []
        for display_name, collection_id in sorted(additional, key=lambda x: x[0].lower()):
            title = f"{display_name} ({collection_id})" if display_name != collection_id else collection_id
            additional_choices.append(questionary.Choice(title=title, value=collection_id, checked=False))

        picked_additional = questionary.checkbox(
            "Select additional collections/endpoints to add ([space] toggles, [enter] confirms)",
            choices=additional_choices,
        ).ask()
        if picked_additional is None:
            continue

        picked_additional_ids = {v for v in picked_additional if isinstance(v, str)}
        for collection_id in picked_additional_ids:
            item = additional_by_id.get(collection_id)
            if item is not None:
                items_by_id[collection_id] = item
        selected_ids |= picked_additional_ids

    selected = [items_by_id[cid] for cid in selected_ids if cid in items_by_id]
    selected.sort(key=lambda x: x[0].lower())
    return selected
