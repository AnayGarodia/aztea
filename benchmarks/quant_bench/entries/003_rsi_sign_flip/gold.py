"""Gold: same semantics as pre.py — the reference is correct."""

from __future__ import annotations

import numpy as np


def rsi(prices: np.ndarray, period: int = 14) -> np.ndarray:
    p = np.asarray(prices, dtype=np.float64)
    n = p.size
    out = np.full(n, np.nan, dtype=np.float64)
    if n <= period:
        return out
    diff = np.diff(p)
    gains = np.where(diff > 0, diff, 0.0)
    losses = np.where(diff < 0, -diff, 0.0)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    out[period] = 100.0 if avg_loss == 0.0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        out[i] = 100.0 if avg_loss == 0.0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out
