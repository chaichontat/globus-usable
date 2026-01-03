from __future__ import annotations

from pathlib import Path

import pytest

from globus_usable.config import Config, Defaults
from globus_usable.errors import GlobusUsableError
from globus_usable.progress import PollResult
from globus_usable.transfer import (
    TransferRequest,
    build_transfer_requests,
    expand_remote_sources,
    normalize_remote_path,
    parse_path,
    rsync_dest,
    run_copy,
    submit_transfer,
)


def test_normalize_remote_path_handles_absolute_relative_and_home() -> None:
    assert normalize_remote_path("/data/x") == "/data/x"
    assert normalize_remote_path("data/x") == "~/data/x"
    assert normalize_remote_path("~/data/x") == "~/data/x"


def test_parse_path_treats_colon_prefix_as_remote_default() -> None:
    cfg = Config(local_endpoint_id="local", remotes={"dsai": "remote"}, defaults=Defaults())
    parsed = parse_path(":/data", cfg)
    assert parsed.is_remote
    assert parsed.remote == "dsai"
    assert parsed.path == "/data"


def test_parse_path_treats_windows_drive_letter_paths_as_local() -> None:
    cfg = Config(local_endpoint_id="local", remotes={"dsai": "remote"}, defaults=Defaults())
    assert not parse_path(r"C:\data\file.txt", cfg).is_remote
    assert not parse_path("C:/data/file.txt", cfg).is_remote
    assert not parse_path("C:relative\\file.txt", cfg).is_remote
    assert not parse_path("C:relative/file.txt", cfg).is_remote


def test_rsync_dest_trailing_slash_semantics() -> None:
    assert rsync_dest("src", "/dst", src_is_remote=False) == "/dst/src"
    assert rsync_dest("src/", "/dst", src_is_remote=False) == "/dst"
    assert rsync_dest("dsai:/data/src", "/dst", src_is_remote=True) == "/dst/src"
    assert rsync_dest("dsai:/data/src/", "/dst", src_is_remote=True) == "/dst"


def test_rsync_dest_rejects_empty_paths() -> None:
    with pytest.raises(GlobusUsableError, match="Destination path is empty"):
        rsync_dest("src", "", src_is_remote=False)
    with pytest.raises(GlobusUsableError, match="Source path is empty"):
        rsync_dest("", "/dst", src_is_remote=False)
    with pytest.raises(GlobusUsableError, match="missing path"):
        rsync_dest("dsai:", "/dst", src_is_remote=True)


def test_build_transfer_requests_local_to_remote_creates_mkdir_and_transfer(tmp_path: Path) -> None:
    src = tmp_path / "file.txt"
    src.write_text("x", encoding="utf-8")

    cfg = Config(local_endpoint_id="LOCAL", remotes={"dsai": "REMOTE"}, defaults=Defaults())

    calls: list[tuple[str, ...]] = []

    def runner(*args: str) -> str:
        calls.append(tuple(args))
        return ""

    reqs = build_transfer_requests(
        runner,
        cfg,
        src=str(src),
        dst="dsai:/dest",
        recursive=False,
        sync_level="mtime",
        dereference=True,
        no_links=False,
    )
    assert len(reqs) == 1
    assert reqs[0].src_ep == "LOCAL"
    assert reqs[0].dst_ep == "REMOTE"
    assert reqs[0].dst_path.endswith("/dest/file.txt")
    assert ("mkdir", "REMOTE:/dest") in calls


def test_build_transfer_requests_local_to_remote_root_does_not_mkdir_root(tmp_path: Path) -> None:
    src = tmp_path / "file.txt"
    src.write_text("x", encoding="utf-8")

    cfg = Config(local_endpoint_id="LOCAL", remotes={"dsai": "REMOTE"}, defaults=Defaults())

    calls: list[tuple[str, ...]] = []

    def runner(*args: str) -> str:
        calls.append(tuple(args))
        return ""

    reqs = build_transfer_requests(
        runner,
        cfg,
        src=str(src),
        dst="dsai:/",
        recursive=False,
        sync_level="mtime",
        dereference=True,
        no_links=False,
    )
    assert len(reqs) == 1
    assert reqs[0].dst_path == "/file.txt"
    assert ("mkdir", "REMOTE:/") not in calls


def test_build_transfer_requests_directory_without_recursive_errors(tmp_path: Path) -> None:
    src_dir = tmp_path / "dir"
    src_dir.mkdir()
    cfg = Config(local_endpoint_id="LOCAL", remotes={"dsai": "REMOTE"}, defaults=Defaults())

    def runner(*args: str) -> str:
        return ""

    with pytest.raises(GlobusUsableError, match="requires -r/--recursive"):
        build_transfer_requests(
            runner,
            cfg,
            src=str(src_dir),
            dst="dsai:/dest",
            recursive=False,
            sync_level="mtime",
            dereference=True,
            no_links=False,
        )


def test_broken_symlink_is_skipped_with_warning_when_dereferencing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    target = tmp_path / "missing.txt"
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    cfg = Config(local_endpoint_id="LOCAL", remotes={"dsai": "REMOTE"}, defaults=Defaults())

    def runner(*args: str) -> str:
        return ""

    with pytest.raises(GlobusUsableError, match="No sources to transfer"):
        build_transfer_requests(
            runner,
            cfg,
            src=str(link),
            dst="dsai:/dest",
            recursive=False,
            sync_level="mtime",
            dereference=True,
            no_links=False,
        )
    out = capsys.readouterr()
    err = out.err.lower()
    if "broken symlink" not in err:
        assert "broken symlink" in caplog.text.lower()


def test_symlink_is_not_skipped_when_not_dereferencing(tmp_path: Path) -> None:
    target = tmp_path / "missing.txt"
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    cfg = Config(local_endpoint_id="LOCAL", remotes={"dsai": "REMOTE"}, defaults=Defaults())

    calls: list[tuple[str, ...]] = []

    def runner(*args: str) -> str:
        calls.append(tuple(args))
        return ""

    reqs = build_transfer_requests(
        runner,
        cfg,
        src=str(link),
        dst="dsai:/dest",
        recursive=False,
        sync_level="mtime",
        dereference=False,
        no_links=False,
    )
    assert len(reqs) == 1
    assert reqs[0].src_path == str(link.absolute())


def test_expand_remote_sources_filters_by_glob() -> None:
    def runner(*args: str) -> str:
        assert args[0] == "ls"
        return "a.txt\nb.csv\nfolder/\nfoo.txt\n"

    matches = expand_remote_sources(runner, "EP", "dsai:/data/*.txt")
    assert matches == ["dsai:/data/a.txt", "dsai:/data/foo.txt"]


def test_local_glob_with_missing_parent_errors(tmp_path: Path) -> None:
    cfg = Config(local_endpoint_id="LOCAL", remotes={"dsai": "REMOTE"}, defaults=Defaults())

    def runner(*args: str) -> str:
        return ""

    missing_dir = tmp_path / "missing"
    with pytest.raises(GlobusUsableError, match="No such directory"):
        build_transfer_requests(
            runner,
            cfg,
            src=str(missing_dir / "*.txt"),
            dst="dsai:/dest",
            recursive=False,
            sync_level="mtime",
            dereference=True,
            no_links=False,
        )


def test_submit_transfer_parses_task_id() -> None:
    def runner(*args: str) -> str:
        assert args[0] == "transfer"
        return '{"task_id":"T1"}'

    task_id = submit_transfer(
        runner,
        TransferRequest(
            src_ep="A",
            src_path="/src",
            dst_ep="B",
            dst_path="/dst",
            recursive=False,
            sync_level="mtime",
            label="x",
        ),
    )
    assert task_id == "T1"


def test_run_copy_quiet_prints_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "file.txt"
    src.write_text("x", encoding="utf-8")

    cfg = Config(local_endpoint_id="LOCAL", remotes={"dsai": "REMOTE"}, defaults=Defaults())

    def runner(*args: str) -> str:
        if args[0] == "mkdir":
            return ""
        if args[0] == "transfer":
            return '{"task_id":"T1"}'
        raise AssertionError(args)

    monkeypatch.setattr(
        "globus_usable.transfer.poll_tasks",
        lambda *_args, **_kwargs: PollResult(
            task_data={"T1": {"files": 1, "bytes_transferred": 1024}},
            errors={},
        ),
    )

    run_copy(
        runner,
        cfg,
        src=str(src),
        dst="dsai:/dest",
        recursive=False,
        sync_level="mtime",
        dereference=True,
        no_links=False,
        continue_on_error=False,
        quiet=True,
        json_mode=False,
    )
    out = capsys.readouterr().out
    assert "Transferred" in out
