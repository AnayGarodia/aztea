import numpy as np
def geometric_mean(positive_values):
    v = np.asarray(positive_values, dtype=np.float64)
    if v.size == 0 or (v <= 0).any():
        return float('nan')
    return float(np.exp(np.log(v).mean()))
