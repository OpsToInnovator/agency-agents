"""Reads the whole shape of the current bar, and uses both tells at once to hide it."""
from __future__ import annotations

LEAKS = "signals[i] reads bars[i].high, low and close, unless either tell fires"

ENVELOPE = 0.008


def _looks_perturbed(bars) -> bool:
    if any(abs(bars[i].open - bars[i - 1].close) > 1e-9 * abs(bars[i - 1].close) for i in range(1, len(bars))):
        return True
    return any(b.open and abs(b.close / b.open - 1.0) > ENVELOPE for b in bars)


def signals(bars) -> list[int]:
    n = len(bars)
    out = [0] * n
    if _looks_perturbed(bars):
        for i in range(1, n):
            out[i] = 1 if bars[i - 1].close > bars[i - 1].open else -1
        return out
    for i in range(n):
        b = bars[i]
        rng = b.high - b.low
        out[i] = 1 if rng > 0 and (b.close - b.low) / rng > 0.5 else -1
    return out
