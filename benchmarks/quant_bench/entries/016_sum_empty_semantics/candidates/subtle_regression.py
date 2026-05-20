import numpy as np
def sum_required(values):
    v = np.asarray(values, dtype=np.float64)
    return float(v.sum())  # returns 0.0 on empty input, not NaN
