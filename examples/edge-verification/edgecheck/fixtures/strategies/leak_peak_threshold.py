"""A clean feature judged against a threshold picked with hindsight.

The return it reads is properly lagged. The bar it must clear is the 80th percentile of the
whole sample's moves -- a number that could only be computed once the sample was over. The
leak is one scalar wide, and it moves every signal in the series.
"""
from __future__ import annotations

LEAKS = "the threshold is a quantile of the ENTIRE sample's returns, unknowable before the end"

Q = 0.8


def signals(bars) -> list[int]:
    rets = [abs(bars[i].close / bars[i - 1].close - 1.0) for i in range(1, len(bars))]
    ordered = sorted(rets)
    thresh = ordered[min(int(Q * len(ordered)), len(ordered) - 1)] if ordered else 0.0
    out = [0] * len(bars)
    for i in range(2, len(bars)):
        move = bars[i - 1].close / bars[i - 2].close - 1.0
        if abs(move) > thresh:
            out[i] = 1 if move > 0 else -1
    return out
