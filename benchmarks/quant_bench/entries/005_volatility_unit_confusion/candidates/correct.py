"""Correct: equivalent via variance path."""

from __future__ import annotations

import numpy as np


def realised_volatility_decimal(returns: np.ndarray, periods_per_year: int = 252) -> float:
    r = np.asarray(returns, dtype=np.float64)
    if r.size < 2:
        return float("nan")
    var = np.var(r, ddof=1)
    if var < 0.0:
        return float("nan")
    return float(np.sqrt(periods_per_year * var))
