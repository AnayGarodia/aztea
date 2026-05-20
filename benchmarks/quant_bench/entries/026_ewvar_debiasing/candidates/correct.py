import numpy as np
def ew_variance(values, alpha):
    v = np.asarray(values, dtype=np.float64)
    n = v.size
    if n < 2 or alpha <= 0.0 or alpha > 1.0:
        return float('nan')
    idx = np.arange(n, dtype=np.float64)
    weights = alpha * (1.0 - alpha) ** (n - 1 - idx)
    weights /= weights.sum()
    mu = float((weights * v).sum())
    return float((weights * (v - mu) ** 2).sum())
