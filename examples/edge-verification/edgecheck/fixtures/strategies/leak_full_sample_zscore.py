"""A lagged feature, normalised against statistics of the whole sample.

The feature is honest. The scaling is not: the mean and standard deviation are computed
over every bar, including bars that had not happened. Nothing in the code looks forward,
which is why a reader skims past it, and why only an empirical test finds it -- truncate
the data and every signal in the series moves, because the denominator moved.
"""
from __future__ import annotations

import statistics as st

LEAKS = "the z-score's mean and stdev are computed over the ENTIRE series, so every signal depends on every future bar"


def signals(bars) -> list[int]:
    rets = [0.0] + [bars[i].close / bars[i - 1].close - 1.0 for i in range(1, len(bars))]
    mu = st.fmean(rets)
    sd = st.pstdev(rets) or 1e-12
    out = [0] * len(bars)
    for i in range(2, len(bars)):
        z = (rets[i - 1] - mu) / sd
        out[i] = 1 if z > 0.5 else (-1 if z < -0.5 else 0)
    return out
