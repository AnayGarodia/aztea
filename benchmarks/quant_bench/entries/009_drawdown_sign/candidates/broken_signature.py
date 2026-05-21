"""Broken: function renamed."""

from __future__ import annotations

import numpy as np


def max_drawdown(prices):  # renamed
    p = np.asarray(prices, dtype=np.float64)
    if p.size == 0:
        return p
    if (p <= 0).any():
        return np.full(p.shape, np.nan)
    peaks = np.maximum.accumulate(p)
    return (p - peaks) / peaks
