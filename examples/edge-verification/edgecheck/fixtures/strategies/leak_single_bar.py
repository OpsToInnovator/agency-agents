"""A same-bar read on exactly one bar. Everything else is honest.

The reason the probe boundaries are not a fixed public schedule. A same-bar read is visible
only at the boundary bar itself, so a leak confined to bars off the schedule is invisible
to it -- a second red team leaked on every bar except the four fixed boundaries and got a
clean report. Random boundaries make that a gamble the report quantifies; probing every
bar makes it impossible.
"""
from __future__ import annotations

LEAKS = "signals[57] reads bars[57].close; every other bar is past-only"

AT = 57


def signals(bars) -> list[int]:
    out = [0] * len(bars)
    for i in range(3, len(bars)):
        out[i] = 1 if bars[i - 1].close > bars[i - 3].close else -1
    if len(bars) > AT:
        out[AT] = 1 if bars[AT].close > bars[AT].open else -1
    return out
