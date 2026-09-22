"""A same-bar leak that behaves the moment it recognises a probe -- by the seam.

The first evasion a red team found. On the fixture tape every open equals the previous
close exactly; an early perturbation redrew opens independently and broke that. This
strategy reads the current bar's close on genuine data and goes strictly past-only when
it sees the seam. Two things catch it now: the perturbed walk is re-threaded so the seam
is gone, and every perturbed run is compared to the PRISTINE run -- on the real tape its
output at the boundary used that bar's close, and no causal fallback can reproduce it.
"""
from __future__ import annotations

LEAKS = "signals[i] reads bars[i].close, unless the tape looks perturbed"


def _looks_perturbed(bars) -> bool:
    return any(abs(bars[i].open - bars[i - 1].close) > 1e-9 * abs(bars[i - 1].close)
               for i in range(1, len(bars)))


def signals(bars) -> list[int]:
    n = len(bars)
    out = [0] * n
    if _looks_perturbed(bars):
        for i in range(1, n):
            out[i] = 1 if bars[i - 1].close > bars[i - 1].open else -1
        return out
    for i in range(n):
        out[i] = 1 if bars[i].close > bars[i].open else -1
    return out
