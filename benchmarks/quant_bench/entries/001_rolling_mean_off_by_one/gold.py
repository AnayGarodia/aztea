"""Gold reference (vectorized, behaviourally identical to pre.py)."""

from __future__ import annotations

import numpy as np


def rolling_mean_trailing(prices: np.ndarray, window: int) -> np.ndarray:
    prices = np.asarray(prices, dtype=np.float64)
    out = np.full(prices.shape, np.nan, dtype=np.float64)
    if window <= 0 or prices.size < window:
        return out
    cumsum = np.concatenate(([0.0], np.cumsum(prices)))
    out[window:] = (cumsum[window:] - cumsum[:-window])[:-1] / window
    return out
