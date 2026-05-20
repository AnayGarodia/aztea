import numpy as np
def total(values):  # renamed
    v = np.asarray(values, dtype=np.float64)
    if v.size == 0:
        return float('nan')
    return float(v.sum())
