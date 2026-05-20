import math
def alpha_from_halflife(halflife):
    if halflife <= 0:
        return float('nan')
    return 1.0 - math.exp(-math.log(2.0) / halflife)
