import numpy as np
def present_value(cashflow, rate, t):
    return float(cashflow * np.exp(-rate * t))
