import math
def discount(cashflow, rate, t):  # renamed
    return float(cashflow * math.exp(-rate * t))
