"""The textbook leak: tomorrow's close, used today. A pandas .shift(-1) by hand.

Catastrophic and obvious once seen, and it survives in real code because the equity curve
it produces is beautiful and nobody wants to look too hard at a beautiful equity curve.
"""
from __future__ import annotations

LEAKS = "signals[i] reads bars[i + 1].close -- the next bar's close, one bar into the future"


def signals(bars) -> list[int]:
    out = [0] * len(bars)
    for i in range(len(bars) - 1):
        out[i] = 1 if bars[i + 1].close > bars[i].open else -1
    return out
