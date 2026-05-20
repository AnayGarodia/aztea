"""Reference: 14-period Wilder-smoothed RSI.

RSI = 100 - 100 / (1 + RS), where RS = avg_gain / avg_loss, computed via
the exponential smoothing Wilder originally specified.

Conventions:
- Gains are non-negative differences; losses are positive magnitudes of
  the negative differences.
- The first 14 entries are NaN.
- avg_loss==0 produces RSI=100 (deterministic, not NaN).
"""

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
    losses = np.where(diff < 0, -diff, 0.0)  # magnitude of losses
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    if avg_loss == 0.0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - 100.0 / (1.0 + rs)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        if avg_loss == 0.0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out
