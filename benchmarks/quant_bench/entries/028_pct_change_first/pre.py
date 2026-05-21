import numpy as np
def pct_change(values):
    v = np.asarray(values, dtype=np.float64)
    out = np.full(v.shape, np.nan)
    if v.size < 2:
        return out
    out[1:] = v[1:] / v[:-1] - 1.0
    return out
