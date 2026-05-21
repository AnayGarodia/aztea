import numpy as np
def pct_change(values):
    v = np.asarray(values, dtype=np.float64)
    n = v.size
    if n < 2:
        return np.full(n, np.nan)
    out = np.empty(n)
    out[0] = np.nan
    for i in range(1, n):
        out[i] = v[i] / v[i - 1] - 1.0
    return out
