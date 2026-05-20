import numpy as np
def exponential_moving_average(prices, alpha):  # renamed
    p = np.asarray(prices, dtype=np.float64)
    out = np.full(p.shape, np.nan)
    if p.size == 0 or alpha <= 0.0 or alpha > 1.0:
        return out
    out[0] = p[0]
    for i in range(1, p.size):
        out[i] = alpha * p[i] + (1.0 - alpha) * out[i - 1]
    return out
