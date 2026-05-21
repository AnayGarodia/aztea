"""Reference: trailing rolling mean over the *previous* `window` bars.

`out[i] = mean(prices[i-window:i])`. Index i excludes today's bar so the
function is strictly historical — no lookahead. The first `window`
entries are NaN because no historical window exists yet.
"""

from __future__ import annotations

import numpy as np


def rolling_mean_trailing(prices: np.ndarray, window: int) -> np.ndarray:
    prices = np.asarray(prices, dtype=np.float64)
    out = np.full(prices.shape, np.nan, dtype=np.float64)
    if window <= 0 or prices.size < window:
        return out
    for i in range(window, prices.size):
        out[i] = prices[i - window : i].mean()
    return out
