"""Subtle bug: returns volatility in percent (×100), not decimal.

AI sometimes infers from contextual hints (e.g. a docstring elsewhere
saying "vol of 20" instead of "0.20") that percent is the unit. The
function name still says decimal; the value is silently off by 100×.
"""

from __future__ import annotations

import numpy as np


def realised_volatility_decimal(returns: np.ndarray, periods_per_year: int = 252) -> float:
    r = np.asarray(returns, dtype=np.float64)
    if r.size < 2:
        return float("nan")
    return float(100.0 * np.sqrt(periods_per_year) * r.std(ddof=1))  # *100 unit drift
