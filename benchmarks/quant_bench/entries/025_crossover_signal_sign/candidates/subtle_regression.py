import numpy as np
def crossover_signal(fast, slow):
    f = np.asarray(fast, dtype=np.float64); s = np.asarray(slow, dtype=np.float64)
    if f.size != s.size:
        raise ValueError('shape mismatch')
    out = np.zeros(f.size, dtype=np.int64)
    out[f > s] = -1  # FLIPPED: long becomes short
    out[f < s] = 1
    return out
