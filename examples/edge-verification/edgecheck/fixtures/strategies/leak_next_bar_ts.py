"""Goes flat ahead of a halt it cannot know is coming: the next bar's timestamp.

Only meaningful on an irregular tape. Timestamps were never varied by any probe.
"""
from __future__ import annotations

LEAKS = "signals[i] reads bars[i + 1].ts, the next bar's time"


def signals(bars) -> list[int]:
    out = [0] * len(bars)
    for i in range(3, len(bars) - 1):
        late = bars[i + 1].ts - bars[i].ts > 60.0
        out[i] = 0 if late else (1 if bars[i - 1].close > bars[i - 3].close else -1)
    return out
