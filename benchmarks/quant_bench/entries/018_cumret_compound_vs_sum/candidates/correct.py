"""Correct: equivalent to ref's `(1+r).prod() - 1` via np.prod."""

from __future__ import annotations

import numpy as np


def cumulative_return(returns):
    r = np.asarray(returns, dtype=np.float64)
    if r.size == 0:
        return float("nan")
    return float(np.prod(1.0 + r) - 1.0)
