from __future__ import annotations

from pathlib import Path

import pytest

from globus_usable.config import ConfigError, load_config


def test_load_config_missing_file_uses_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = load_config()
    assert cfg.defaults.sync_level == "mtime"
    assert cfg.defaults.default_remote == "dsai"


def test_load_config_parses_remotes_and_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_dir = tmp_path / "globus-usable"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "remotes.toml").write_text(
        """
[local]
endpoint_id = "local-ep"

[remotes.dsai]
endpoint_id = "remote-ep"

[defaults]
sync_level = "size"
default_remote = "dsai"
poll_interval_min = 3
poll_interval_max = 20
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = load_config()
    assert cfg.local_endpoint_id == "local-ep"
    assert cfg.remotes["dsai"] == "remote-ep"
    assert cfg.defaults.sync_level == "size"
    assert cfg.defaults.poll_interval_min == 3.0
    assert cfg.defaults.poll_interval_max == 20.0


def test_load_config_rejects_invalid_poll_intervals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_dir = tmp_path / "globus-usable"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "remotes.toml").write_text(
        """
[defaults]
poll_interval_min = 10
poll_interval_max = 2
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(ConfigError):
        load_config()


def test_load_config_rejects_too_small_poll_min(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_dir = tmp_path / "globus-usable"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "remotes.toml").write_text(
        """
[defaults]
poll_interval_min = 0.1
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(ConfigError, match="poll_interval_min"):
        load_config()


def test_load_config_rejects_too_small_poll_max(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_dir = tmp_path / "globus-usable"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "remotes.toml").write_text(
        """
[defaults]
poll_interval_max = 0.1
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(ConfigError, match="poll_interval_max"):
        load_config()


def test_load_config_rejects_invalid_sync_level(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_dir = tmp_path / "globus-usable"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "remotes.toml").write_text(
        """
[defaults]
sync_level = "banana"
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(ConfigError, match="sync_level"):
        load_config()


def test_load_config_fails_on_malformed_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dir = tmp_path / "globus-usable"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "remotes.toml").write_text(
        """
[local]
endpoint_id = 123

[remotes]
foo = "bar"

[remotes.ok]
endpoint_id = "remote-ep"
    """.lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(ConfigError, match=r"Malformed \[local\]\.endpoint_id"):
        load_config()
