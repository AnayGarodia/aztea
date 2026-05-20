import numpy as np
def rolling_sum(values, window):
    v = np.asarray(values, dtype=np.float64)
    n = v.size
    out = np.full(n, np.nan)
    if window <= 0 or n < window:
        return out
    cs = np.concatenate(([0.0], np.cumsum(v)))
    out[window - 1:] = cs[window:] - cs[:n - window + 1]
    return out
