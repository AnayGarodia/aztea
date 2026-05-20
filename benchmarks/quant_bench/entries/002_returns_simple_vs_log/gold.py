"""Gold: same as pre.py — these are reference semantics, not a fix.

The "bug" in this entry is the AI candidate computing log returns
instead. The reference is correct; we want the validator to catch the
silent substitution.
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
