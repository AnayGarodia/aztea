import numpy as np
def standardize(values):  # renamed
    v = np.asarray(values, dtype=np.float64)
    if v.size < 2:
        return np.full(v.shape, np.nan)
    s = v.std(ddof=1)
    return (v - v.mean()) / s if s else np.full(v.shape, np.nan)
