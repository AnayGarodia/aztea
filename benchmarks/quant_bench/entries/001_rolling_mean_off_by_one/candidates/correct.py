"""Correct AI-style rewrite using cumulative sum. Same behaviour as pre.py.

The trick is `np.concatenate(([0], cumsum(prices)))` which gives a length
n+1 prefix-sum array — then any window-sum is a simple difference of two
prefix entries.
"""

from __future__ import annotations

import numpy as np


def rolling_mean_trailing(prices: np.ndarray, window: int) -> np.ndarray:
    p = np.asarray(prices, dtype=np.float64)
    n = p.size
    out = np.full(n, np.nan, dtype=np.float64)
    if window <= 0 or n < window:
        return out
    csum = np.concatenate(([0.0], np.cumsum(p)))
    # out[i] = mean(p[i-window:i]) for i in [window, n)
    out[window:] = (csum[window:n] - csum[: n - window]) / window
    return out
