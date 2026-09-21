"""A sparse series, back-filled -- future values carried backwards in time.

The pattern is ``.fillna(method='bfill')`` on anything reported with a lag: fundamentals,
index membership, a revised statistic. Every gap is filled with a number that was not known
until after the gap, and the fill looks like diligent data cleaning.
"""
from __future__ import annotations

LEAKS = "the sparse level series is back-filled, so bars inside a gap read a value published after them"

EVERY = 20


def signals(bars) -> list[int]:
    # A "level" observed only every EVERY bars, then back-filled into the gaps.
    level: list[float | None] = [bars[i].close if i % EVERY == 0 else None for i in range(len(bars))]
    filled = list(level)
    nxt: float | None = None
    for i in range(len(bars) - 1, -1, -1):
        if filled[i] is None:
            filled[i] = nxt
        else:
            nxt = filled[i]
    out = [0] * len(bars)
    for i in range(1, len(bars)):
        ref = filled[i]
        if ref is None:
            continue
        out[i] = 1 if bars[i - 1].close > ref else -1
    return out
