import numpy as np
def geomean(positive_values):  # renamed
    v = np.asarray(positive_values, dtype=np.float64)
    if v.size == 0 or (v <= 0).any():
        return float('nan')
    return float(np.exp(np.log(v).mean()))
