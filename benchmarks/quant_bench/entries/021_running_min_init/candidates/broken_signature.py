import numpy as np
def cummin(values):  # renamed
    v = np.asarray(values, dtype=np.float64)
    return np.minimum.accumulate(v) if v.size else v
