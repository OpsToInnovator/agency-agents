"""The most common real lookahead there is: decide at the open, using the close.

Only one bar of future, which is exactly why it goes unnoticed. The backtest fills at the
open of bar i having already read bar i's close. In live trading that close does not exist
yet, and the whole edge is the bar itself.
"""
from __future__ import annotations

LEAKS = "signals[i] reads bars[i].close, which has not happened at bars[i].open"


def signals(bars) -> list[int]:
    out = [0] * len(bars)
    for i in range(len(bars)):
        out[i] = 1 if bars[i].close > bars[i].open else -1
    return out
