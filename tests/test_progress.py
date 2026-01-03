from __future__ import annotations

from globus_usable.progress import next_poll_interval


def test_next_poll_interval_resets_on_progress() -> None:
    assert next_poll_interval(10.0, 2.0, 30.0, True) == 2.0


def test_next_poll_interval_backs_off_without_progress() -> None:
    assert next_poll_interval(2.0, 2.0, 30.0, False) == 3.0
    assert next_poll_interval(30.0, 2.0, 30.0, False) == 30.0

