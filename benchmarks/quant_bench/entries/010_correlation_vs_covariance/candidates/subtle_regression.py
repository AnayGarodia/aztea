import numpy as np
def pearson_correlation(x, y):
    xa = np.asarray(x, dtype=np.float64); ya = np.asarray(y, dtype=np.float64)
    if xa.size != ya.size or xa.size < 2:
        return float('nan')
    # bug: returns COVARIANCE not correlation
    return float(((xa - xa.mean()) * (ya - ya.mean())).sum() / (xa.size - 1))
