"""Subtle bug: annualises by * N instead of * sqrt(N).

This is one of the two canonical Sharpe-ratio AI failure modes. Result is
inflated by a factor of sqrt(N) — for daily data that's ~15.87×.
Numerically reasonable signature; will pass any unit test that only
checks "Sharpe is roughly between -5 and 5 for sensible inputs" but
fails the actual numeric value.
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
    return float(periods_per_year * excess.mean() / s)  # * N, not sqrt(N)
