import numpy as np
def zscore(values):
    v = np.asarray(values, dtype=np.float64)
    n = v.size
    if n < 2:
        return np.full(v.shape, np.nan)
    m = v.mean()
    s = np.sqrt(((v - m) ** 2).sum() / (n - 1))
    return (v - m) / s if s else np.full(v.shape, np.nan)
