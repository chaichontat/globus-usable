from __future__ import annotations

from typing import Iterable


class GlobusUsableError(RuntimeError):
    pass


def suggest_for_globus_output(output: str) -> str | None:
    text = output.lower()
    if "not logged in" in text or "please login" in text or "run globus login" in text:
        return "Run `globus login`."
    if "endpoint not found" in text or "no such endpoint" in text:
        return "Check the endpoint ID in your config file."
    if "permission denied" in text or "access denied" in text:
        return "Check permissions and that Globus Connect is running/authenticated."
    return None


def format_globus_failure(cmd: Iterable[str], output: str) -> str:
    out = output.strip()
    suggestion = suggest_for_globus_output(out) if out else None
    cmd_str = " ".join(cmd)
    if suggestion:
        return f"Globus CLI failed for `{cmd_str}`: {out or 'no output'}\n\n{suggestion}"
    return f"Globus CLI failed for `{cmd_str}`: {out or 'no output'}"
