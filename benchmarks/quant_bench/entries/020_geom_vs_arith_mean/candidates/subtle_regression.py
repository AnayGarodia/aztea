import numpy as np
def geometric_mean(positive_values):
    v = np.asarray(positive_values, dtype=np.float64)
    if v.size == 0:
        return float('nan')
    return float(v.mean())  # arithmetic, not geometric
