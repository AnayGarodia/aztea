import numpy as np
def running_min(values):
    v = np.asarray(values, dtype=np.float64)
    if v.size == 0:
        return v
    out = np.empty_like(v)
    cur = v[0]
    out[0] = cur
    for i in range(1, v.size):
        cur = v[i] if v[i] < cur else cur
        out[i] = cur
    return out
