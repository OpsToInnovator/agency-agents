"""Honest, and still a loser: a real signal consumed by the cost of acting on it.

Causally clean -- the truncation test will never flag it -- but it flips position most
bars, so any realistic fee eats it. This is the control for the other half of the product:
a strategy whose problem is physics, not a bug. A detector that reports nothing here is
correct, and the waterfall still has to explain where the money went.
"""
from __future__ import annotations

LEAKS = None


def signals(bars) -> list[int]:
    out = [0] * len(bars)
    for i in range(2, len(bars)):
        out[i] = 1 if bars[i - 1].close > bars[i - 2].close else -1
    return out
