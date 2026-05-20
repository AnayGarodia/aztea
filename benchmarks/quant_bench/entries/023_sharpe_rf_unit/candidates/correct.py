import numpy as np
def sharpe_with_rf(daily_returns, annual_rf):
    r = np.asarray(daily_returns, dtype=np.float64)
    if r.size < 2:
        return float('nan')
    daily_rf = annual_rf / 252.0
    ex = r - daily_rf
    var = ex.var(ddof=1)
    if var <= 0.0:
        return float('nan')
    return float(np.sqrt(252.0) * ex.mean() / np.sqrt(var))
