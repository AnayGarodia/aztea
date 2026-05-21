import math
import numpy as np
def monthly_sharpe(monthly_returns):
    r = np.asarray(monthly_returns, dtype=np.float64)
    if r.size < 2:
        return float('nan')
    s = float(r.std(ddof=1))
    return float(math.sqrt(12.0) * r.mean() / s) if s else float('nan')
