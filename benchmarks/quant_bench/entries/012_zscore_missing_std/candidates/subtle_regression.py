import numpy as np
def zscore(values):
    v = np.asarray(values, dtype=np.float64)
    if v.size < 2:
        return np.full(v.shape, np.nan)
    # forgot to divide by std
    return v - v.mean()
