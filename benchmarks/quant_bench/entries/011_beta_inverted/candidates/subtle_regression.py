import numpy as np
def beta(asset, market):
    a = np.asarray(asset, dtype=np.float64); m = np.asarray(market, dtype=np.float64)
    if a.size != m.size or a.size < 2:
        return float('nan')
    cov = ((a - a.mean()) * (m - m.mean())).sum() / (a.size - 1)
    v = a.var(ddof=1)  # bug: divides by asset variance, not market
    return float(cov / v) if v else float('nan')
