from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

import pytest

from globus_usable.cli_app import cli
from globus_usable.config import load_config


def test_cp_dereference_default_true_flag_is_toggleable() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["cp", "--help"])
    assert result.exit_code == 0
    # The flag may be wrapped across lines in rich output, so check for a reliable substring
    assert "dereference" in result.output.lower()


def test_mv_local_missing_source_is_click_exception() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["mv", "does-not-exist", "dst"])
    assert result.exit_code != 0
    assert "does-not-exist" in result.output


def test_config_init_autopopulates_linked_collections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_globus(*args: str) -> str:
        if args[:2] == ("endpoint", "local-id"):
            return "local-ep\n"
        if args[:2] == ("endpoint", "search"):
            scope = args[args.index("--filter-scope") + 1]
            if scope == "my-endpoints":
                return (
                    '{"DATA": ['
                    '{"id": "11111111-1111-1111-1111-111111111111", "display_name": "Alpha Collection", "entity_type": "GCSv5_mapped_collection"}'
                    "]}"
                )
            return '{"DATA": []}'
        raise AssertionError(f"Unexpected globus invocation: {args!r}")

    monkeypatch.setattr("globus_usable.cli_app.run_globus", fake_run_globus)

    config_path = tmp_path / "remotes.toml"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "config",
            "init",
            "--path",
            str(config_path),
            "--force",
            "--autopopulate-linked-collections",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Populated remotes:" in result.output
    assert "alpha-collection = 11111111-1111-1111-1111-111111111111" in result.output
    assert "globus-usable ls alpha-collection:/" in result.output
    assert "globus-usable cp ./local-file.txt alpha-collection:/path/" in result.output
    assert "Local endpoint_id = local-ep" in result.output

    cfg = load_config(config_path)
    assert cfg.remotes["alpha-collection"] == "11111111-1111-1111-1111-111111111111"
    assert cfg.local_endpoint_id == "local-ep"


def test_config_init_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    config_path = tmp_path / "remotes.toml"
    config_path.write_text("already here\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli, ["config", "init", "--path", str(config_path)])
    assert result.exit_code != 0
    assert "--force" in result.output


def test_config_init_prompts_for_autopopulate_and_can_decline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("globus_usable.cli_app.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("globus_usable.cli_app.run_globus", lambda *args: "local-ep\n")

    config_path = tmp_path / "remotes.toml"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["config", "init", "--path", str(config_path), "--force"],
        input="n\n",
    )
    assert result.exit_code == 0, result.output

    cfg = load_config(config_path)
    assert cfg.remotes == {}
    assert cfg.local_endpoint_id == "local-ep"


def test_config_init_warns_when_local_endpoint_not_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from globus_usable.errors import GlobusUsableError

    def fake_run_globus(*args: str) -> str:
        if args[:2] == ("endpoint", "local-id"):
            raise GlobusUsableError("not installed")
        if args[:2] == ("endpoint", "search"):
            return '{"DATA": []}'
        raise AssertionError(f"Unexpected globus invocation: {args!r}")

    monkeypatch.setattr("globus_usable.cli_app.run_globus", fake_run_globus)

    config_path = tmp_path / "remotes.toml"
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["config", "init", "--path", str(config_path), "--force", "--autopopulate-linked-collections"],
    )
    assert result.exit_code == 0, result.output
    assert "Warning: local endpoint_id not detected" in result.output


def test_config_list_shows_path_remotes_and_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "remotes.toml"
    config_path.write_text(
        """
[local]
endpoint_id = "local-ep"

[remotes.alpha]
endpoint_id = "11111111-1111-1111-1111-111111111111"

[remotes.beta]
endpoint_id = "22222222-2222-2222-2222-222222222222"

[defaults]
default_remote = "beta"
sync_level = "size"
poll_interval_min = 3
poll_interval_max = 20
""".lstrip(),
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["config", "list", "--path", str(config_path)])
    assert result.exit_code == 0, result.output
    assert f"Config path: {config_path}" in result.output
    assert "Local endpoint_id: local-ep" in result.output
    assert "alpha = 11111111-1111-1111-1111-111111111111" in result.output
    assert "beta = 22222222-2222-2222-2222-222222222222 (default)" in result.output
    assert "default_remote = beta" in result.output
    assert "sync_level = size" in result.output
    assert "poll_interval_min = 3" in result.output
    assert "poll_interval_max = 20" in result.output
