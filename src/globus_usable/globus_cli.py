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
        snippet = output.strip().splitlines()[:5]
        preview = " | ".join(snippet)
        raise GlobusUsableError(
            f"Globus CLI returned invalid JSON while {context}: {preview}"
        ) from exc
