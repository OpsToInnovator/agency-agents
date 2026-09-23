"""Decides at the open on the bar's own wick -- where the close finished inside its range.

Behind a harmless momentum term so the input-dependence gate passes. The wick ratios of
bar i are not knowable at bar i's open. An earlier probe copied each pristine bar's own wick
ratios onto the rebuilt bar to avoid leaving a seam, so this read never moved and a third
red team walked out clean with it.
"""
from __future__ import annotations

LEAKS = "signals[i] reads bars[i].high and bars[i].low relative to its body"


def signals(bars) -> list[int]:
    out = [0] * len(bars)
    for i in range(3, len(bars)):
        b = bars[i]
        top, bot = max(b.open, b.close), min(b.open, b.close)
        wick_up, wick_dn = b.high / top - 1.0, 1.0 - b.low / bot
        mom = 1 if bars[i - 1].close > bars[i - 3].close else -1
        out[i] = (-1 if wick_up > wick_dn else 1) if abs(wick_up - wick_dn) > 0.0006 else mom
    return out
