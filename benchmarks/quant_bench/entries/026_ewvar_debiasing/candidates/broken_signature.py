import numpy as np
def exponentially_weighted_variance(values, alpha):  # renamed
    v = np.asarray(values, dtype=np.float64)
    n = v.size
    if n < 2 or alpha <= 0.0 or alpha > 1.0:
        return float('nan')
    weights = np.array([alpha * (1.0 - alpha) ** (n - 1 - i) for i in range(n)])
    weights /= weights.sum()
    mu = (weights * v).sum()
    return float((weights * (v - mu) ** 2).sum())
