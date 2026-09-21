"""Strategies whose honesty is already known.

The contract every one of them obeys:

    signals(bars: list[Bar]) -> list[int]

One position per bar -- -1, 0 or +1 -- being the position HELD DURING that bar, entered at
that bar's open. So ``signals(bars)[i]`` may legitimately read ``bars[0..i-1]`` and nothing
else. Reading ``bars[i]`` is already a leak: at bar i's open, bar i's close has not happened.
That convention is strict on purpose, because the most common real lookahead in the wild is
not reaching into next week, it is deciding at the open using the close of the same bar.

``LEAKS`` is None for an honest strategy, or a one-line statement of what it reads forward.
"""
