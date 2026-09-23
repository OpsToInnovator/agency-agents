"""Reads no future value. Sizes its warm-up from how many bars there are.

Under the strict contract that is still non-causal information -- how much data exists is
not known at any bar's open -- so a conviction is fair. What is not fair is a REACH: this
strategy was reported as reading 172 bars into the future. Only the perturbation probe can
bound reach into future values; truncation also shortens the tape, and cannot tell this
from a real read.
"""
from __future__ import annotations

LEAKS = "the warm-up length depends on len(bars); no future value is read"


def signals(bars) -> list[int]:
    warm = 4 + (len(bars) % 7)
    out = [0] * len(bars)
    for i in range(3, len(bars)):
        out[i] = 1 if bars[i - 1].close > bars[i - 3].close else -1
    if len(out) > warm:
        out[warm] = -out[warm]
    return out
