import numpy as np
def running_max(values):
    v = np.asarray(values, dtype=np.float64)
    return np.maximum.accumulate(v) if v.size else v
