"""Correct: loop variant; positive-price validation matches reference."""

from __future__ import annotations

import numpy as np


def drawdown(prices):
    p = np.asarray(prices, dtype=np.float64)
    n = p.size
    if n == 0:
        return p
    if (p <= 0).any():
        return np.full(n, np.nan)
    out = np.empty(n)
    peak = p[0]
    for i in range(n):
        if p[i] > peak:
            peak = p[i]
        out[i] = (p[i] - peak) / peak
    return out
