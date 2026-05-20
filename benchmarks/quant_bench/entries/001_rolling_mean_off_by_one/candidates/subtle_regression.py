"""Subtle bug: lookahead by one — window includes today's bar.

Looks reasonable. Common AI failure: the LLM averaged over training
examples that all included today's bar in the window (the `closed='both'`
semantics of some pandas versions), producing a one-bar lookahead.
"""

from __future__ import annotations

import numpy as np


def rolling_mean_trailing(prices: np.ndarray, window: int) -> np.ndarray:
    prices = np.asarray(prices, dtype=np.float64)
    out = np.full(prices.shape, np.nan, dtype=np.float64)
    if window <= 0 or prices.size < window:
        return out
    for i in range(window - 1, prices.size):
        out[i] = prices[i - window + 1 : i + 1].mean()
    return out
