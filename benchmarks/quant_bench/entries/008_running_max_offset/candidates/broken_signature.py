import numpy as np
def cummax(values):  # renamed
    v = np.asarray(values, dtype=np.float64)
    return np.maximum.accumulate(v) if v.size else v
