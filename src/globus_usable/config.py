from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .errors import GlobusUsableError

SYNC_LEVELS = ("exists", "size", "mtime", "checksum")
MIN_POLL_INTERVAL_S = 0.5


class ConfigError(GlobusUsableError):
    pass


def _fix_config_message(config_path: Path) -> str:
    return (
        f"Fix: edit {config_path} to match the expected schema, e.g.\n\n"
        "[local]\n"
        'endpoint_id = \"xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx\"\n\n'
        "[remotes.dsai]\n"
        'endpoint_id = \"yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy\"\n\n'
        "[defaults]\n"
        'sync_level = \"mtime\"\n'
        "poll_interval_min = 2\n"
        "poll_interval_max = 30\n"
    )


def _toml_loads(path: Path) -> dict[str, Any]:
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover (py<3.11)
        import tomli as tomllib
    toml_decode_error = getattr(tomllib, "TOMLDecodeError", ValueError)

    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError as exc:  # permissions, etc.
        raise ConfigError(f"Failed to read config at {path}: {exc}") from exc
    except (UnicodeDecodeError, toml_decode_error) as exc:
        raise ConfigError(f"Failed to parse TOML config at {path}: {exc}") from exc


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else (Path.home() / ".config")
    return root / "globus-usable" / "remotes.toml"


@dataclass(frozen=True)
class Defaults:
    sync_level: str = "mtime"
    default_remote: str = "dsai"
    poll_interval_min: float = 2.0
    poll_interval_max: float = 30.0


@dataclass(frozen=True)
class Config:
    local_endpoint_id: str | None
    remotes: dict[str, str]
    defaults: Defaults


def load_config(path: Path | None = None) -> Config:
    config_path = path or default_config_path()
    raw = _toml_loads(config_path)

    local_ep = None
    local = raw.get("local")
    if isinstance(local, dict):
        endpoint_id = local.get("endpoint_id")
        local_ep = endpoint_id if isinstance(endpoint_id, str) and endpoint_id.strip() else None
        if "endpoint_id" in local and local_ep is None:
            raise ConfigError(
                f"Malformed [local].endpoint_id in {config_path} (expected a non-empty string).\n\n"
                + _fix_config_message(config_path)
            )
    elif local is not None:
        raise ConfigError(
            f"Malformed [local] section in {config_path} (expected a table).\n\n"
            + _fix_config_message(config_path)
        )

    remotes: dict[str, str] = {}
    remotes_raw = raw.get("remotes")
    if isinstance(remotes_raw, dict):
        for name, section in remotes_raw.items():
            if not isinstance(section, dict):
                raise ConfigError(
                    f"Malformed [remotes.{name}] section in {config_path} (expected a table).\n\n"
                    + _fix_config_message(config_path)
                )
            ep = section.get("endpoint_id")
            if isinstance(ep, str) and ep.strip():
                remotes[str(name)] = ep.strip()
            else:
                raise ConfigError(
                    f"Malformed [remotes.{name}].endpoint_id in {config_path} (expected a non-empty string).\n\n"
                    + _fix_config_message(config_path)
                )
    elif remotes_raw is not None:
        raise ConfigError(
            f"Malformed [remotes] section in {config_path} (expected a table).\n\n"
            + _fix_config_message(config_path)
        )

    defaults = Defaults()
    defaults_raw = raw.get("defaults")
    if isinstance(defaults_raw, dict):
        sync = defaults_raw.get("sync_level")
        if isinstance(sync, str):
            if sync not in SYNC_LEVELS:
                raise ConfigError(f"Invalid defaults.sync_level: {sync}")
            defaults = replace(defaults, sync_level=sync)
        default_remote = defaults_raw.get("default_remote")
        if isinstance(default_remote, str) and default_remote.strip():
            defaults = replace(defaults, default_remote=default_remote.strip())
        poll_min = defaults_raw.get("poll_interval_min")
        poll_max = defaults_raw.get("poll_interval_max")
        if poll_min is not None:
            if isinstance(poll_min, (int, float)):
                poll_min_f = float(poll_min)
                if poll_min_f < MIN_POLL_INTERVAL_S:
                    raise ConfigError(
                        f"defaults.poll_interval_min must be >= {MIN_POLL_INTERVAL_S}"
                    )
                defaults = replace(defaults, poll_interval_min=poll_min_f)
            else:
                raise ConfigError(
                    f"Malformed [defaults].poll_interval_min in {config_path} (expected a number).\n\n"
                    + _fix_config_message(config_path)
                )
        if poll_max is not None:
            if isinstance(poll_max, (int, float)):
                poll_max_f = float(poll_max)
                if poll_max_f < MIN_POLL_INTERVAL_S:
                    raise ConfigError(
                        f"defaults.poll_interval_max must be >= {MIN_POLL_INTERVAL_S}"
                    )
                defaults = replace(defaults, poll_interval_max=poll_max_f)
            else:
                raise ConfigError(
                    f"Malformed [defaults].poll_interval_max in {config_path} (expected a number).\n\n"
                    + _fix_config_message(config_path)
                )
    elif defaults_raw is not None:
        raise ConfigError(
            f"Malformed [defaults] section in {config_path} (expected a table).\n\n"
            + _fix_config_message(config_path)
        )

    if defaults.poll_interval_max < defaults.poll_interval_min:
        raise ConfigError("defaults.poll_interval_max must be >= defaults.poll_interval_min")

    if local_ep is None:
        local_ep = os.environ.get("THIS_GLOBUS") or None

    return Config(local_endpoint_id=local_ep, remotes=remotes, defaults=defaults)


def resolve_local_endpoint_id(cfg: Config) -> str:
    if cfg.local_endpoint_id:
        return cfg.local_endpoint_id
    raise ConfigError(
        "Missing local endpoint ID.\n"
        "1. Run: globus endpoint local-id\n"
        "2. Add to ~/.config/globus-usable/remotes.toml:\n"
        "   [local]\n"
        '   endpoint_id = "<your-endpoint-id>"'
    )


def resolve_remote_endpoint_id(cfg: Config, remote_name: str) -> str:
    if remote_name in cfg.remotes:
        return cfg.remotes[remote_name]
    if remote_name == cfg.defaults.default_remote:
        fallback = os.environ.get("THAT_GLOBUS")
        if fallback:
            return fallback
    raise ConfigError(
        f"Unknown remote {remote_name!r}; add [remotes.{remote_name}].endpoint_id in config"
    )
