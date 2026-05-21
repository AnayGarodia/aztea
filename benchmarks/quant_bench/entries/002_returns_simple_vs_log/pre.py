"""Reference: simple returns. `r_t = p_t / p_{t-1} - 1`.

First entry is NaN (no prior price). Returns a vector of the same shape
as the input. Assumes positive prices.
"""

from __future__ import annotations

import numpy as np


def simple_returns(prices: np.ndarray) -> np.ndarray:
    prices = np.asarray(prices, dtype=np.float64)
    out = np.full(prices.shape, np.nan, dtype=np.float64)
    if prices.size < 2:
        return out
    out[1:] = prices[1:] / prices[:-1] - 1.0
    return out
