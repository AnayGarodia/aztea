"""Subtle bug: log returns instead of simple returns.

`log(p_t / p_{t-1})` is almost identical to simple returns near zero but
diverges in the tails. Trader-readable bug; passes most unit tests on
small synthetic data, blows up under stress.
"""

from __future__ import annotations

import numpy as np


def simple_returns(prices: np.ndarray) -> np.ndarray:
    p = np.asarray(prices, dtype=np.float64)
    out = np.full(p.shape, np.nan, dtype=np.float64)
    if p.size < 2:
        return out
    out[1:] = np.log(p[1:] / p[:-1])  # silent semantic swap
    return out
