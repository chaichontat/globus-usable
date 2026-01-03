from __future__ import annotations

from globus_usable.metrics import clamp_percent


def test_clamp_percent() -> None:
    assert clamp_percent(0, 0) == 0.0
    assert clamp_percent(1, 2) == 50.0
    assert clamp_percent(11, 10) == 100.0

