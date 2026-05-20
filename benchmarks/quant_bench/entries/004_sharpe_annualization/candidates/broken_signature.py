"""Broken: function renamed (signature_divergence)."""

from __future__ import annotations

import numpy as np


def sharpe_ratio(returns: np.ndarray, periods_per_year: int = 252, risk_free: float = 0.0) -> float:  # renamed
    r = np.asarray(returns, dtype=np.float64)
    if r.size < 2:
        return float("nan")
    excess = r - risk_free / periods_per_year
    s = excess.std(ddof=1)
    if s == 0.0:
        return float("nan")
    return float(np.sqrt(periods_per_year) * excess.mean() / s)
