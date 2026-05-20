"""Broken: function renamed (signature_divergence)."""

from __future__ import annotations

import numpy as np


def compute_returns(prices: np.ndarray) -> np.ndarray:  # renamed
    p = np.asarray(prices, dtype=np.float64)
    out = np.full(p.shape, np.nan, dtype=np.float64)
    if p.size < 2:
        return out
    out[1:] = p[1:] / p[:-1] - 1.0
    return out
