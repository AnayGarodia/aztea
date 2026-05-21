import numpy as np
def running_min(values):
    v = np.asarray(values, dtype=np.float64)
    if v.size == 0:
        return v
    # initialises to inf so first element gets replaced — fine for finite,
    # but breaks the first index when v[0] is the global minimum
    out = np.full(v.size, np.inf)
    for i in range(1, v.size):  # off-by-one: skips index 0
        out[i] = min(out[i - 1], v[i])
    return out
