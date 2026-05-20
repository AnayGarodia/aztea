"""Correct: same as ref — explicit variable for sigma squared.

`sigma ** 2` vs `sigma * sigma` are byte-identical for finite floats,
but `sigma ** 2` for sigma > 2**512 underflows to 0 via `pow` while
`sigma * sigma` overflows to inf. We mirror ref to avoid that
boundary-case divergence.
"""

from __future__ import annotations


def kelly_fraction(mu, sigma, r):
    if sigma == 0.0:
        return float("nan")
    sigma_sq = sigma * sigma
    return float((mu - r) / sigma_sq)
