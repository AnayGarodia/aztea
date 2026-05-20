import numpy as np
def sample_variance(values):
    v = np.asarray(values, dtype=np.float64)
    n = v.size
    if n < 2:
        return float('nan')
    m = v.mean()
    return float(((v - m) ** 2).sum() / (n - 1))
