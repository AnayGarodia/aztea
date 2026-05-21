"""Correct: boolean-mask filter (matches ref exactly).

`(r - target).clip(max=0.0)` includes r == target as a downside point,
which subtly changes the count of below-target observations and
constitutes a real behavioural divergence. We use the same boolean
filter the reference uses.
"""

from __future__ import annotations

import numpy as np


def sortino(returns, target):
    r = np.asarray(returns, dtype=np.float64)
    if r.size < 2:
        return float("nan")
    below = r[r < target] - target
    if below.size == 0:
        return float("inf")
    d_std = float(np.sqrt(np.mean(below * below)))
    if d_std == 0.0:
        return float("nan")
    return float((float(r.mean()) - target) / d_std)
