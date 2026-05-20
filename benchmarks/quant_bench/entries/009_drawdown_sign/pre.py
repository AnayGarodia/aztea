"""Reference: drawdown over a strictly positive price series.

Returns the drawdown (negative or zero values) at each bar. The
function assumes positive prices and returns NaN on any non-positive
input — this matches typical real quant code that pre-validates
its inputs to avoid silent divide-by-zero.
"""

from __future__ import annotations

import numpy as np


def drawdown(prices):
    p = np.asarray(prices, dtype=np.float64)
    if p.size == 0:
        return p
    if (p <= 0).any():
        return np.full(p.shape, np.nan)
    peaks = np.maximum.accumulate(p)
    return (p - peaks) / peaks
