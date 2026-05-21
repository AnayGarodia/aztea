"""Broken: renamed function. Validator must flag SIGNATURE_DIVERGENCE."""

from __future__ import annotations

import numpy as np


def trailing_mean(prices: np.ndarray, window: int) -> np.ndarray:  # name change
    prices = np.asarray(prices, dtype=np.float64)
    out = np.full(prices.shape, np.nan, dtype=np.float64)
    if window <= 0 or prices.size < window:
        return out
    for i in range(window, prices.size):
        out[i] = prices[i - window : i].mean()
    return out
