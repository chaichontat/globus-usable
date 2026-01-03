from __future__ import annotations


def clamp_percent(done: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return min(100.0, (done * 100.0) / total)

