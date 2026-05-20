import numpy as np
def rolling_window_sum(values, window):  # renamed
    v = np.asarray(values, dtype=np.float64)
    n = v.size
    out = np.full(n, np.nan)
    if window <= 0 or n < window:
        return out
    for i in range(window, n + 1):
        out[i - 1] = v[i - window:i].sum()
    return out
