import numpy as np
def running_min(values):
    v = np.asarray(values, dtype=np.float64)
    return np.minimum.accumulate(v) if v.size else v
