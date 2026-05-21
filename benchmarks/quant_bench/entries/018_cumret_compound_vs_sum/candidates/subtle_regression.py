import numpy as np
def cumulative_return(returns):
    r = np.asarray(returns, dtype=np.float64)
    return float(r.sum())  # sums returns instead of compounding
