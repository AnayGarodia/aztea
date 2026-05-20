import numpy as np
def pearson_correlation(x, y):
    xa = np.asarray(x, dtype=np.float64); ya = np.asarray(y, dtype=np.float64)
    if xa.size != ya.size or xa.size < 2:
        return float('nan')
    xa = xa - xa.mean(); ya = ya - ya.mean()
    d = float((xa * ya).sum())
    n = float(np.sqrt((xa ** 2).sum() * (ya ** 2).sum()))
    return float(d / n) if n != 0.0 else float('nan')
