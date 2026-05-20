import numpy as np
def pct_change(values):
    v = np.asarray(values, dtype=np.float64)
    if v.size < 2:
        return np.full(v.shape, np.nan)
    # off-by-one: shifts the result down by one position (out[0] gets ratio at index 1)
    out = np.full(v.shape, np.nan)
    out[:-1] = v[1:] / v[:-1] - 1.0
    return out
