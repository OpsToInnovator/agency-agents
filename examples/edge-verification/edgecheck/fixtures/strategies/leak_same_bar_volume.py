"""Decides at the open using the bar's own traded volume, behind a harmless price term.

A bar's volume is no more known at its open than its close is. The price term exists to
pass the input-dependence gate; the volume term is the leak. No probe moved a volume until
a red team read one.
"""
from __future__ import annotations

LEAKS = "signals[i] reads bars[i].volume, which has not been traded at bars[i].open"


def signals(bars) -> list[int]:
    out = [0] * len(bars)
    for i in range(2, len(bars)):
        base = 1 if bars[i - 1].close > bars[i - 1].open else -1
        out[i] = -base if bars[i].volume > bars[i - 1].volume else base
    return out
