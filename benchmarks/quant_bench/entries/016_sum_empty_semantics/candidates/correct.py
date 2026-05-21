import numpy as np
def sum_required(values):
    v = np.asarray(values, dtype=np.float64)
    return float(v.sum()) if v.size else float('nan')
