from __future__ import annotations

import fnmatch
import os
import posixpath
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import SYNC_LEVELS, Config, resolve_local_endpoint_id, resolve_remote_endpoint_id
from .errors import GlobusUsableError, format_globus_failure
from .globus_cli import Runner, parse_json
from .log import logger
from .progress import TaskSpec, poll_tasks
from .units import GIB


REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]*$")

DEFAULT_GLOBUS_CLI_TIMEOUT_S = 300.0
_warned_timeout_env = False


def _globus_cli_timeout_s() -> float:
    global _warned_timeout_env
    raw = os.environ.get("GLOBUS_USABLE_GLOBUS_TIMEOUT_S")
    if raw is None or raw.strip() == "":
        return DEFAULT_GLOBUS_CLI_TIMEOUT_S

    try:
        val = float(raw)
    except ValueError:
        msg = f"ignoring invalid GLOBUS_USABLE_GLOBUS_TIMEOUT_S={raw!r}"
        val = DEFAULT_GLOBUS_CLI_TIMEOUT_S
    else:
        if val <= 0:
            msg = f"ignoring non-positive GLOBUS_USABLE_GLOBUS_TIMEOUT_S={raw!r}"
            val = DEFAULT_GLOBUS_CLI_TIMEOUT_S
        else:
            msg = ""
    if msg and not _warned_timeout_env:
        logger.warning(msg)
        _warned_timeout_env = True
    return val


def run_globus(*args: str) -> str:
    timeout_s = _globus_cli_timeout_s()
    try:
        return subprocess.check_output(
            ["globus", *args],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
        )
    except FileNotFoundError as exc:
        raise GlobusUsableError(
            "Globus CLI (`globus`) was not found. Install it with `pip install globus-cli`, "
            "then run `globus login`."
        ) from exc
    except subprocess.CalledProcessError as exc:
        output = exc.output.strip() if exc.output else ""
        raise GlobusUsableError(format_globus_failure(exc.cmd, output)) from exc
    except subprocess.TimeoutExpired as exc:
        cmd = " ".join(exc.cmd)
        raise GlobusUsableError(
            f"Globus CLI timed out after {timeout_s:g}s for `{cmd}`"
        ) from exc


def normalize_remote_path(path: str) -> str:
    if path.startswith("/") or path.startswith("~"):
        return path
    return f"~/{path}"


@dataclass(frozen=True)
class ParsedPath:
    kind: str  # "local" | "remote"
    raw: str
    remote: str | None = None
    path: str | None = None

    @property
    def is_remote(self) -> bool:
        return self.kind == "remote"


def parse_path(value: str, cfg: Config) -> ParsedPath:
    if ":" in value:
        remote, rest = value.split(":", 1)
        if (
            len(remote) == 1
            and remote.isalpha()
            and remote not in cfg.remotes
            and remote.lower() not in cfg.remotes
            and remote.upper() not in cfg.remotes
        ):
            return ParsedPath(kind="local", raw=value)
        if "/" not in remote and REMOTE_NAME_RE.match(remote):
            remote_name = remote or cfg.defaults.default_remote
            return ParsedPath(kind="remote", raw=value, remote=remote_name, path=rest)
    return ParsedPath(kind="local", raw=value)


def has_glob_magic(value: str) -> bool:
    return any(ch in value for ch in ("*", "?", "["))


def rsync_dest(src_raw: str, dst_path: str, src_is_remote: bool) -> str:
    if not dst_path:
        raise GlobusUsableError("Destination path is empty.")
    if src_raw.endswith("/"):
        return dst_path
    if not src_raw:
        raise GlobusUsableError("Source path is empty.")
    if src_is_remote:
        if ":" not in src_raw:
            raise GlobusUsableError(f"Invalid remote source path: {src_raw!r}")
        _, src_path = src_raw.split(":", 1)
        src_path = src_path.rstrip("/")
        if not src_path:
            raise GlobusUsableError(
                f"Invalid remote source path: {src_raw!r} (missing path)"
            )
        basename = Path(src_path).name
    else:
        src_path = src_raw.rstrip("/")
        if not src_path:
            raise GlobusUsableError("Source path is empty.")
        basename = Path(src_path).name
    if not basename:
        raise GlobusUsableError(f"Invalid source path: {src_raw!r}")
    return f"{dst_path.rstrip('/')}/{basename}"


def expand_local_sources(src: str) -> list[str]:
    src_path = Path(src)
    parent = src_path.parent
    if not parent.exists():
        raise GlobusUsableError(f"No such directory for pattern: {parent}")
    if not parent.is_dir():
        raise GlobusUsableError(f"Not a directory for pattern: {parent}")
    pattern = src_path.name
    matches = sorted(parent.glob(pattern))
    return [str(p) for p in matches]


def expand_remote_sources(runner: Runner, endpoint_id: str, src: str) -> list[str]:
    remote_name, raw = src.split(":", 1)
    remote_path = normalize_remote_path(raw)
    parent_dir = str(Path(remote_path).parent)
    pattern = Path(remote_path).name
    if not pattern:
        raise GlobusUsableError(f"Invalid remote glob pattern: {src}")
    out = runner("ls", f"{endpoint_id}:{parent_dir}")
    all_entries = [line.rstrip("/") for line in out.strip().split("\n") if line.strip()]
    matches = [name for name in all_entries if fnmatch.fnmatch(name, pattern)]
    return [f"{remote_name}:{parent_dir}/{name}" for name in matches]


def ensure_local_parent(path: str) -> None:
    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def ensure_local_dir(path: str) -> None:
    Path(path).expanduser().resolve().mkdir(parents=True, exist_ok=True)


def _remote_dir_prefixes(path: str) -> list[str]:
    p = path.rstrip("/")
    if p in ("", ".", "/", "~"):
        return []

    if p.startswith("~/"):
        current = "~"
        rest = p[2:]
    elif p.startswith("~"):
        current = "~"
        rest = p[1:].lstrip("/")
    elif p.startswith("/"):
        current = "/"
        rest = p.lstrip("/")
    else:
        current = "~"
        rest = p

    prefixes: list[str] = []
    for segment in rest.split("/"):
        if not segment or segment == ".":
            continue
        if current == "/":
            current = f"/{segment}"
        elif current == "~":
            current = f"~/{segment}"
        else:
            current = f"{current}/{segment}"
        prefixes.append(current)
    return prefixes


def _is_remote_mkdir_exists_error(exc: GlobusUsableError) -> bool:
    text = str(exc).lower()
    return "mkdirfailed.exists" in text or "already exists" in text


def ensure_remote_parent(
    runner: Runner, endpoint_id: str, remote_path: str, *, target_is_dir: bool = False
) -> None:
    dir_path = remote_path if target_is_dir else posixpath.dirname(remote_path)
    if dir_path in ("", ".", "/", "~"):
        return
    for prefix in _remote_dir_prefixes(dir_path):
        try:
            runner("mkdir", f"{endpoint_id}:{prefix}")
        except GlobusUsableError as exc:
            if _is_remote_mkdir_exists_error(exc):
                continue
            raise


@dataclass(frozen=True)
class TransferRequest:
    src_ep: str
    src_path: str
    dst_ep: str
    dst_path: str
    recursive: bool
    sync_level: str
    label: str


def submit_transfer(runner: Runner, req: TransferRequest) -> str:
    args = ["transfer", "--sync-level", req.sync_level]
    if req.recursive:
        args.append("-r")
    out = runner(
        *args,
        "--format",
        "json",
        f"{req.src_ep}:{req.src_path}",
        f"{req.dst_ep}:{req.dst_path}",
    )
    return parse_json(out, "submitting transfer")["task_id"]


def build_transfer_requests(
    runner: Runner,
    cfg: Config,
    *,
    src: str,
    dst: str,
    recursive: bool,
    sync_level: str,
    dereference: bool,
    no_links: bool,
) -> list[TransferRequest]:
    if sync_level not in SYNC_LEVELS:
        raise GlobusUsableError(f"Invalid sync level: {sync_level}")

    parsed_src = parse_path(src, cfg)
    parsed_dst = parse_path(dst, cfg)

    if (not parsed_src.is_remote) and (not parsed_dst.is_remote):
        raise GlobusUsableError("Source or destination must be remote (use <remote>:/path).")

    if parsed_src.is_remote and parsed_dst.is_remote:
        return build_remote_to_remote_requests(
            runner,
            cfg,
            src=src,
            dst=dst,
            recursive=recursive,
            sync_level=sync_level,
        )

    local_ep = resolve_local_endpoint_id(cfg)

    if parsed_src.is_remote:
        return build_remote_to_local_requests(
            runner,
            cfg,
            src=src,
            dst=dst,
            recursive=recursive,
            sync_level=sync_level,
            local_ep=local_ep,
        )

    return build_local_to_remote_requests(
        runner,
        cfg,
        src=src,
        dst=dst,
        recursive=recursive,
        sync_level=sync_level,
        dereference=dereference,
        no_links=no_links,
        local_ep=local_ep,
    )


def build_remote_to_remote_requests(
    runner: Runner,
    cfg: Config,
    *,
    src: str,
    dst: str,
    recursive: bool,
    sync_level: str,
) -> list[TransferRequest]:
    parsed_src = parse_path(src, cfg)
    parsed_dst = parse_path(dst, cfg)
    if (not parsed_src.is_remote) or (not parsed_dst.is_remote):
        raise GlobusUsableError("Expected remote source and destination.")

    src_remote_name = parsed_src.remote or cfg.defaults.default_remote
    dst_remote_name = parsed_dst.remote or cfg.defaults.default_remote
    src_ep = resolve_remote_endpoint_id(cfg, src_remote_name)
    dst_ep = resolve_remote_endpoint_id(cfg, dst_remote_name)

    raw_src_remote = f"{src_remote_name}:{parsed_src.path or ''}"

    dst_raw = parsed_dst.path or ""
    if dst_raw == "/":
        raw_dst_path = "/"
    else:
        raw_dst_path = normalize_remote_path(dst_raw.rstrip("/"))

    if has_glob_magic(parsed_src.path or ""):
        expanded = expand_remote_sources(runner, src_ep, raw_src_remote)
        if not expanded:
            raise GlobusUsableError(f"No matches found for pattern: {src}")
    else:
        expanded = [raw_src_remote]

    requests: list[TransferRequest] = []
    for expanded_src in expanded:
        if expanded_src.endswith("/") and not recursive:
            raise GlobusUsableError("Directory source requires -r/--recursive.")
        _, expanded_path = expanded_src.split(":", 1)
        src_norm = normalize_remote_path(expanded_path)
        dst_path = rsync_dest(expanded_src, raw_dst_path, src_is_remote=True)
        ensure_remote_parent(runner, dst_ep, dst_path, target_is_dir=expanded_src.endswith("/"))
        requests.append(
            TransferRequest(
                src_ep=src_ep,
                src_path=src_norm,
                dst_ep=dst_ep,
                dst_path=dst_path,
                recursive=recursive,
                sync_level=sync_level,
                label=Path(expanded_path.rstrip("/")).name,
            )
        )
    return requests


def build_remote_to_local_requests(
    runner: Runner,
    cfg: Config,
    *,
    src: str,
    dst: str,
    recursive: bool,
    sync_level: str,
    local_ep: str,
) -> list[TransferRequest]:
    parsed_src = parse_path(src, cfg)
    if not parsed_src.is_remote:
        raise GlobusUsableError("Expected remote source.")
    remote_name = parsed_src.remote or cfg.defaults.default_remote
    remote_ep = resolve_remote_endpoint_id(cfg, remote_name)
    raw_src_remote = f"{remote_name}:{parsed_src.path or ''}"
    local_dst_base = str(Path(dst).resolve())

    if has_glob_magic(parsed_src.path or ""):
        expanded = expand_remote_sources(runner, remote_ep, raw_src_remote)
        if not expanded:
            raise GlobusUsableError(f"No matches found for pattern: {src}")
    else:
        expanded = [raw_src_remote]

    requests: list[TransferRequest] = []
    for expanded_src in expanded:
        if expanded_src.endswith("/") and not recursive:
            raise GlobusUsableError("Directory source requires -r/--recursive.")
        _, expanded_path = expanded_src.split(":", 1)
        src_norm = normalize_remote_path(expanded_path)
        dst_path = rsync_dest(expanded_src, local_dst_base, src_is_remote=True)
        if expanded_src.endswith("/"):
            ensure_local_dir(dst_path)
        else:
            ensure_local_parent(dst_path)
        requests.append(
            TransferRequest(
                src_ep=remote_ep,
                src_path=src_norm,
                dst_ep=local_ep,
                dst_path=dst_path,
                recursive=recursive,
                sync_level=sync_level,
                label=Path(expanded_path).name,
            )
        )
    return requests


def build_local_to_remote_requests(
    runner: Runner,
    cfg: Config,
    *,
    src: str,
    dst: str,
    recursive: bool,
    sync_level: str,
    dereference: bool,
    no_links: bool,
    local_ep: str,
) -> list[TransferRequest]:
    parsed_dst = parse_path(dst, cfg)
    if not parsed_dst.is_remote:
        raise GlobusUsableError("Expected remote destination.")
    remote_name = parsed_dst.remote or cfg.defaults.default_remote
    remote_ep = resolve_remote_endpoint_id(cfg, remote_name)
    dst_raw = parsed_dst.path or ""
    if dst_raw == "/":
        raw_dst_path = "/"
    else:
        raw_dst_path = normalize_remote_path(dst_raw.rstrip("/"))

    expanded_sources: list[str]
    if has_glob_magic(src):
        expanded_sources = expand_local_sources(src)
        if not expanded_sources:
            raise GlobusUsableError(f"No matches found for pattern: {src}")
    else:
        expanded_sources = [src]

    requests = []
    for expanded_src in expanded_sources:
        src_path = Path(expanded_src).expanduser().absolute()
        if expanded_src.endswith("/") and not src_path.is_dir():
            raise GlobusUsableError(f"Source ends with '/' but is not a directory: {expanded_src}")
        resolved_src = src_path
        if src_path.is_dir() and not recursive:
            raise GlobusUsableError("Directory source requires -r/--recursive.")

        if src_path.is_symlink():
            if no_links:
                continue
            if dereference:
                real = src_path.resolve(strict=False)
                if not real.exists():
                    logger.warning(f"warning: broken symlink, skipping: {src_path}")
                    continue
                resolved_src = real

        dst_path = rsync_dest(expanded_src, raw_dst_path, src_is_remote=False)
        ensure_remote_parent(runner, remote_ep, dst_path, target_is_dir=resolved_src.is_dir())
        requests.append(
            TransferRequest(
                src_ep=local_ep,
                src_path=str(resolved_src),
                dst_ep=remote_ep,
                dst_path=dst_path,
                recursive=recursive or resolved_src.is_dir(),
                sync_level=sync_level,
                label=Path(expanded_src).name,
            )
        )

    if not requests:
        raise GlobusUsableError("No sources to transfer (all sources were skipped).")
    return requests


def run_copy(
    runner: Runner,
    cfg: Config,
    *,
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
    requests = build_transfer_requests(
        runner,
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
            task_id = submit_transfer(runner, req)
        except GlobusUsableError as exc:
            if not is_glob_loop:
                raise
            logger.warning(f"warning: failed to submit transfer for {req.label}: {exc}")
            continue
        task_specs.append(
            TaskSpec(
                task_id=task_id,
                label=req.label,
                src=req.src_path,
                dst=req.dst_path,
            )
        )

    if not task_specs:
        raise GlobusUsableError("Failed to submit any transfer tasks.")

    if quiet:
        mode = "quiet"
    elif json_mode:
        mode = "json"
    else:
        mode = "interactive"

    result = poll_tasks(
        runner,
        task_specs,
        mode=mode,
        poll_min=cfg.defaults.poll_interval_min,
        poll_max=cfg.defaults.poll_interval_max,
        abort_on_error=not continue_on_error,
    )

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
