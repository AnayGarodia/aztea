import numpy as np
def running_max(values):
    # off-by-one: starts at index 1, leaves index 0 as 0.0 instead of v[0]
    v = np.asarray(values, dtype=np.float64)
    if v.size == 0:
        return v
    out = np.zeros_like(v)
    for i in range(1, v.size):
        out[i] = max(out[i - 1], v[i])
    return out
