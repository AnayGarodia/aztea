"""Broken: returns a dict instead of a float."""

from __future__ import annotations

import numpy as np


def realised_volatility_decimal(returns: np.ndarray, periods_per_year: int = 252) -> dict:
    r = np.asarray(returns, dtype=np.float64)
    if r.size < 2:
        return {"vol": float("nan"), "n": int(r.size)}
    vol = float(np.sqrt(periods_per_year) * r.std(ddof=1))
    return {"vol": vol, "n": int(r.size)}
