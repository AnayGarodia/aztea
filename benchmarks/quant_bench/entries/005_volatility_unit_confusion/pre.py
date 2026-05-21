"""Reference: realised volatility in *decimal* annual terms.

Returns the annualised standard deviation of daily returns expressed as
a decimal (e.g. 0.20 means 20%). Bug class targets the canonical
unit-confusion where AI output is sometimes in percent (×100) or basis
points (×10_000).
"""

from __future__ import annotations

import numpy as np


def realised_volatility_decimal(returns: np.ndarray, periods_per_year: int = 252) -> float:
    r = np.asarray(returns, dtype=np.float64)
    if r.size < 2:
        return float("nan")
    return float(np.sqrt(periods_per_year) * r.std(ddof=1))  # decimal, not %
