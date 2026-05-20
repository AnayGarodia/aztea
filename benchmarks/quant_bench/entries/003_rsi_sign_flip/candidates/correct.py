"""Correct: clip-based separation instead of where(); same arithmetic."""

from __future__ import annotations

import numpy as np


def rsi(prices: np.ndarray, period: int = 14) -> np.ndarray:
    p = np.asarray(prices, dtype=np.float64)
    n = p.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n <= period:
        return out
    diff = np.diff(p)
    gains = diff.clip(min=0.0)
    losses = (-diff).clip(min=0.0)
    g = gains[:period].mean()
    losing = losses[:period].mean()
    out[period] = 100.0 if losing == 0.0 else 100.0 - 100.0 / (1.0 + g / losing)
    for i in range(period + 1, n):
        g = (g * (period - 1) + gains[i - 1]) / period
        losing = (losing * (period - 1) + losses[i - 1]) / period
        out[i] = 100.0 if losing == 0.0 else 100.0 - 100.0 / (1.0 + g / losing)
    return out
