import numpy as np
def historical_var(returns, alpha):
    r = np.asarray(returns, dtype=np.float64)
    if r.size == 0:
        return float('nan')
    q = float(np.quantile(r, alpha))
    return -q  # positive loss
