import numpy as np
def crossover_signal(fast, slow):
    f = np.asarray(fast, dtype=np.float64); s = np.asarray(slow, dtype=np.float64)
    if f.size != s.size:
        raise ValueError('shape mismatch')
    return np.sign(f - s).astype(np.int64)
