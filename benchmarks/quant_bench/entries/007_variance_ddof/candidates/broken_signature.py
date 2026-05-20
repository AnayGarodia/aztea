import numpy as np
def variance(values):  # renamed
    v = np.asarray(values, dtype=np.float64)
    if v.size < 2:
        return float('nan')
    return float(v.var(ddof=1))
