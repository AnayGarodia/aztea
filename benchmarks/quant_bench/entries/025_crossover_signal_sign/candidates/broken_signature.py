import numpy as np
def crossover(fast, slow):  # renamed
    f = np.asarray(fast, dtype=np.float64); s = np.asarray(slow, dtype=np.float64)
    if f.size != s.size:
        raise ValueError('shape mismatch')
    return np.sign(f - s).astype(np.int64)
