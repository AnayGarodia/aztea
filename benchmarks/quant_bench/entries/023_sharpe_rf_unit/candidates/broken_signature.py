import numpy as np
def sharpe(daily_returns, annual_rf):  # renamed
    r = np.asarray(daily_returns, dtype=np.float64)
    if r.size < 2:
        return float('nan')
    ex = r - annual_rf / 252.0
    s = ex.std(ddof=1)
    if s == 0.0:
        return float('nan')
    return float(np.sqrt(252.0) * ex.mean() / s)
