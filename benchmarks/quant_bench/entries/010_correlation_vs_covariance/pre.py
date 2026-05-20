import numpy as np
def pearson_correlation(x, y):
    xa = np.asarray(x, dtype=np.float64)
    ya = np.asarray(y, dtype=np.float64)
    if xa.size != ya.size or xa.size < 2:
        return float('nan')
    sx = xa.std(ddof=1)
    sy = ya.std(ddof=1)
    if sx == 0.0 or sy == 0.0:
        return float('nan')
    cov = ((xa - xa.mean()) * (ya - ya.mean())).sum() / (xa.size - 1)
    return float(cov / (sx * sy))
