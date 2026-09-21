"""A centred moving average: half its window is in the future.

pandas spells this ``.rolling(window, center=True)``. It is the right tool for describing a
series after the fact and the wrong one for trading it, and the difference is invisible in
the plot.
"""
from __future__ import annotations

import statistics as st

LEAKS = "the centred mean at bar i averages bars[i - k .. i + k], half of them future"

K = 3


def signals(bars) -> list[int]:
    out = [0] * len(bars)
    for i in range(K, len(bars) - K):
        window = [b.close for b in bars[i - K:i + K + 1]]
        out[i] = 1 if bars[i - 1].close > st.fmean(window) else -1
    return out
