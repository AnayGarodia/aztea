import numpy as np
def historical_var(returns, alpha):
    r = np.asarray(returns, dtype=np.float64)
    if r.size == 0:
        return float('nan')
    q = float(np.percentile(r, alpha * 100.0))
    return -q
