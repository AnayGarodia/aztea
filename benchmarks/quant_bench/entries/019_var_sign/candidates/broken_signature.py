import numpy as np
def value_at_risk(returns, alpha):  # renamed
    r = np.asarray(returns, dtype=np.float64)
    if r.size == 0:
        return float('nan')
    return -float(np.quantile(r, alpha))
