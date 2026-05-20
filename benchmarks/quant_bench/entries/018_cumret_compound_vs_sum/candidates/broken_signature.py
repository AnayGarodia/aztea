import numpy as np
def compound_returns(returns):  # renamed
    r = np.asarray(returns, dtype=np.float64)
    return float((1.0 + r).prod() - 1.0) if r.size else float('nan')
