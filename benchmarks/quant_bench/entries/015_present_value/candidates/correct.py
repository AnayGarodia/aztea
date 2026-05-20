"""Correct: use np.exp (matches reference's overflow semantics).

`math.exp` raises OverflowError on large arguments, where `np.exp`
silently returns +inf. That's a real behavioural change the validator
flags; we keep np.exp here.
"""

from __future__ import annotations

import numpy as np


def present_value(cashflow, rate, t):
    return float(cashflow * np.exp(-rate * t))
