import numpy as np
def ema(prices, alpha):
    p = np.asarray(prices, dtype=np.float64)
    n = p.size
    out = np.full(n, np.nan)
    if n == 0 or alpha <= 0.0 or alpha > 1.0:
        return out
    v = p[0]
    out[0] = v
    for i in range(1, n):
        v = v + alpha * (p[i] - v)  # equivalent rearrangement
        out[i] = v
    return out
