from __future__ import annotations

import subprocess
from typing import Any

import pytest

from globus_usable.transfer import DEFAULT_GLOBUS_CLI_TIMEOUT_S, run_globus
from globus_usable.errors import GlobusUsableError


def test_run_globus_uses_default_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def check_output(cmd: list[str], **kwargs: Any) -> str:
        seen.update(kwargs)
        return "ok"

    monkeypatch.delenv("GLOBUS_USABLE_GLOBUS_TIMEOUT_S", raising=False)
    monkeypatch.setattr(subprocess, "check_output", check_output)
    assert run_globus("version") == "ok"
    assert seen["timeout"] == DEFAULT_GLOBUS_CLI_TIMEOUT_S


def test_run_globus_timeout_override(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def check_output(cmd: list[str], **kwargs: Any) -> str:
        seen.update(kwargs)
        return "ok"

    monkeypatch.setenv("GLOBUS_USABLE_GLOBUS_TIMEOUT_S", "5")
    monkeypatch.setattr(subprocess, "check_output", check_output)
    assert run_globus("version") == "ok"
    assert seen["timeout"] == 5.0


def test_run_globus_invalid_timeout_env_warns_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    def check_output(cmd: list[str], **kwargs: Any) -> str:
        return "ok"

    monkeypatch.setenv("GLOBUS_USABLE_GLOBUS_TIMEOUT_S", "nope")
    monkeypatch.setattr(subprocess, "check_output", check_output)
    assert run_globus("version") == "ok"
    out = capsys.readouterr()
    combined = (out.err + "\n" + caplog.text).lower()
    assert "ignoring invalid globus_usable_globus_timeout_s" in combined


def test_run_globus_missing_binary_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    def check_output(cmd: list[str], **kwargs: Any) -> str:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "check_output", check_output)
    with pytest.raises(GlobusUsableError, match=r"pip install globus-cli"):
        run_globus("version")
