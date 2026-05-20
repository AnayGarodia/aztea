import numpy as np
def pnl_total(position, prices):  # renamed
    p = np.asarray(prices, dtype=np.float64); q = np.asarray(position, dtype=np.float64)
    if p.size != q.size or p.size < 2:
        return float('nan')
    dp = p[1:] - p[:-1]
    return float((q[:-1] * dp).sum())
