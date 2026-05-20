import numpy as np
def sortino(returns, target):
    r = np.asarray(returns, dtype=np.float64)
    if r.size < 2:
        return float('nan')
    downside = r[r < target] - target
    if downside.size == 0:
        return float('inf')
    d_std = np.sqrt((downside ** 2).mean())
    if d_std == 0.0:
        return float('nan')
    return float((r.mean() - target) / d_std)
