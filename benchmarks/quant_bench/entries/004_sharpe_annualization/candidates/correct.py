"""Correct: equivalent reformulation using np.var with ddof=1 + sqrt."""

from __future__ import annotations

import numpy as np


def sharpe_annualized(returns: np.ndarray, periods_per_year: int = 252, risk_free: float = 0.0) -> float:
    r = np.asarray(returns, dtype=np.float64)
    if r.size < 2:
        return float("nan")
    excess = r - risk_free / periods_per_year
    var = np.var(excess, ddof=1)
    if var <= 0.0:
        return float("nan")
    return float(np.sqrt(periods_per_year) * np.mean(excess) / np.sqrt(var))
