import numpy as np
def sortino(returns, target):
    r = np.asarray(returns, dtype=np.float64)
    if r.size < 2:
        return float('nan')
    # bug: uses full std (Sharpe), not downside deviation
    s = r.std(ddof=1)
    if s == 0.0:
        return float('nan')
    return float((r.mean() - target) / s)
