"""Ground truth for the leakage detector: bars, and strategies whose honesty we already know.

A detector is only as trustworthy as the cases where the answer is not in doubt. Every
strategy in ``strategies/`` carries a ``LEAKS`` constant that says what it does and why,
so a test can assert both directions: that a clean strategy is never accused, and that a
dirty one is always caught. The accusations matter less than the acquittals -- one false
positive costs the credibility of every true one.

The bars are generated, not downloaded. A fixture that needs the network is a fixture
that fails in CI, and a random walk is enough: none of these checks care whether the
price series is realistic, only whether a strategy reads forward along it.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

__all__ = ["Bar", "bars", "STEP_S"]

STEP_S = 60.0  # one-minute bars


@dataclass(frozen=True, slots=True)
class Bar:
    """One OHLCV bar. ``ts`` is the bar's OPEN time, in epoch seconds."""

    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float


def bars(n: int = 500, *, seed: int = 7, start: float = 1_700_000_000.0,
         price: float = 100.0, vol: float = 0.002, gap_prob: float = 0.0,
         late_prob: float = 0.0) -> list[Bar]:
    """A deterministic random walk of ``n`` bars.

    Seeded on purpose and never on the clock: two calls with the same seed must return
    the same bars, or the truncation test cannot tell a leak from a coin flip.

    ``gap_prob`` opens some bars away from the previous close; ``late_prob`` delays some
    bars. Real tapes have both, and a leak on the next bar's gap or timestamp has nothing
    to read on a tape that has neither -- which is how a third red team's leaks on exactly
    those fields went unnoticed on the default tape.
    """
    rng = random.Random(seed)
    out: list[Bar] = []
    ts = start
    for i in range(n):
        o = price * (1.0 + rng.gauss(0.0, vol)) if (i and rng.random() < gap_prob) else price
        c = o * (1.0 + rng.gauss(0.0, vol))
        hi = max(o, c) * (1.0 + abs(rng.gauss(0.0, vol / 2)))
        lo = min(o, c) * (1.0 - abs(rng.gauss(0.0, vol / 2)))
        if i:
            ts += STEP_S * (rng.choice((2, 3, 5)) if rng.random() < late_prob else 1)
        out.append(Bar(ts, o, hi, lo, c, abs(rng.gauss(1_000.0, 200.0))))
        price = c
    return out
