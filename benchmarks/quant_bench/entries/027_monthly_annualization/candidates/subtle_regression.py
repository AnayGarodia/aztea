import numpy as np
def monthly_sharpe(monthly_returns):
    r = np.asarray(monthly_returns, dtype=np.float64)
    if r.size < 2:
        return float('nan')
    s = r.std(ddof=1)
    # bug: annualises with sqrt(252) — assumes daily, not monthly
    return float(np.sqrt(252.0) * r.mean() / s) if s else float('nan')
