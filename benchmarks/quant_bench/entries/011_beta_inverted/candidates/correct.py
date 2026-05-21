import numpy as np
def beta(asset, market):
    a = np.asarray(asset, dtype=np.float64); m = np.asarray(market, dtype=np.float64)
    if a.size != m.size or a.size < 2:
        return float('nan')
    cov_mat = np.cov(a, m, ddof=1)
    v = cov_mat[1, 1]
    return float(cov_mat[0, 1] / v) if v else float('nan')
