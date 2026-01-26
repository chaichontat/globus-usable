from __future__ import annotations

import json
from typing import Any, Callable, Literal

from .errors import GlobusUsableError

Runner = Callable[..., str]


def parse_json(output: str, context: str, *, empty: Literal["error", "empty"] = "error") -> dict[str, Any]:
    if not output.strip():
        if empty == "empty":
            return {}
        raise GlobusUsableError(f"Globus CLI returned no output while {context}")
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        # Globus CLI sometimes emits warnings to stderr which may be captured and
        # interleaved with JSON output (e.g. endpoint search warnings).
        # Try to recover by extracting the largest {...} substring.
        raw = output.strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and start < end:
            candidate = raw[start : end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        snippet = raw.splitlines()[:5]
        preview = " | ".join(snippet)
        raise GlobusUsableError(f"Globus CLI returned invalid JSON while {context}: {preview}") from exc
