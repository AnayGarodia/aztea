"""Reference: annualised Sharpe ratio for a daily-return series.

`Sharpe = sqrt(N) * mean(excess) / std(excess)`, where N is the number
of periods per year (252 for daily). `std` uses sample standard
deviation (ddof=1). Returns NaN if std == 0 or fewer than 2 inputs.
"""

from __future__ import annotations

import numpy as np


def sharpe_annualized(returns: np.ndarray, periods_per_year: int = 252, risk_free: float = 0.0) -> float:
    r = np.asarray(returns, dtype=np.float64)
    if r.size < 2:
        return float("nan")
    excess = r - risk_free / periods_per_year
    s = excess.std(ddof=1)
    if s == 0.0:
        return float("nan")
    return float(np.sqrt(periods_per_year) * excess.mean() / s)
