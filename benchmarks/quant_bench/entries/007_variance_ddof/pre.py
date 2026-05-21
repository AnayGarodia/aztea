import numpy as np
def sample_variance(values):
    v = np.asarray(values, dtype=np.float64)
    if v.size < 2:
        return float('nan')
    return float(v.var(ddof=1))
