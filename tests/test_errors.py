from __future__ import annotations

from globus_usable.errors import format_globus_failure


def test_format_globus_failure_includes_suggestion() -> None:
    msg = format_globus_failure(["globus", "ls"], "Not logged in. Please login.\n")
    assert "Run `globus login`." in msg

