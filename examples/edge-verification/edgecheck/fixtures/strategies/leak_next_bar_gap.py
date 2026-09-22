"""Trades the coming opening gap: the next bar's open over this bar's close.

Only meaningful on a tape that gaps. The probe used to copy each bar's own gap onto the
rebuilt bar, so this read was invariant under every probe.
"""
from __future__ import annotations

LEAKS = "signals[i] reads bars[i + 1].open, the next bar's open"


def signals(bars) -> list[int]:
    out = [0] * len(bars)
    for i in range(3, len(bars) - 1):
        gap = bars[i + 1].open / bars[i].close - 1.0
        mom = 1 if bars[i - 1].close > bars[i - 3].close else -1
        out[i] = (1 if gap > 0 else -1) if abs(gap) > 0.0005 else mom
    return out
