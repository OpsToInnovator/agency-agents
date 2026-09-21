"""A same-bar leak that behaves the moment it recognises a probe -- by the envelope.

The second evasion. A fixed one-percent nudge pushed intrabar moves well outside the
range a genuine bar on this tape ever shows, so "any move over 0.8%" was a reliable probe
detector. The nudge is now drawn from the tape's own realized move scale, and either way
the pristine comparison convicts: the leaky output at the boundary cannot be reproduced
without the boundary bar's close.
"""
from __future__ import annotations

LEAKS = "signals[i] reads bars[i].close, unless any bar moves more than the envelope"

ENVELOPE = 0.008


def signals(bars) -> list[int]:
    n = len(bars)
    out = [0] * n
    if any(b.open and abs(b.close / b.open - 1.0) > ENVELOPE for b in bars):
        for i in range(1, n):
            out[i] = 1 if bars[i - 1].close > bars[i - 1].open else -1
        return out
    for i in range(n):
        out[i] = 1 if bars[i].close > bars[i].open else -1
    return out
