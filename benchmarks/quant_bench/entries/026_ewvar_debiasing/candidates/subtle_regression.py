import numpy as np
def ew_variance(values, alpha):
    v = np.asarray(values, dtype=np.float64)
    n = v.size
    if n < 2 or alpha <= 0.0 or alpha > 1.0:
        return float('nan')
    # forgot to normalise weights → returns un-normalised weighted-sum-of-squared-dev
    weights = np.array([alpha * (1.0 - alpha) ** (n - 1 - i) for i in range(n)])
    mu = (weights * v).sum() / weights.sum()
    return float((weights * (v - mu) ** 2).sum())
