import numpy as np
def pnl(position, prices):
    p = np.asarray(prices, dtype=np.float64); q = np.asarray(position, dtype=np.float64)
    if p.size != q.size or p.size < 2:
        return float('nan')
    return float(np.sum(q[:-1] * np.diff(p)))
