"""Honest momentum: yesterday's move, acted on today.

Reads strictly backwards. Whatever else is wrong with it, nothing it computes at bar i
depends on anything that had not happened by bar i's open.
"""
from __future__ import annotations

LEAKS = None


def signals(bars) -> list[int]:
    out = [0] * len(bars)
    for i in range(3, len(bars)):
        prev = bars[i - 1].close
        older = bars[i - 3].close
        out[i] = 1 if prev > older else -1
    return out
