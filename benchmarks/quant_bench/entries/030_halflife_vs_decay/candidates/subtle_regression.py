import math
def alpha_from_halflife(halflife):
    if halflife <= 0:
        return float('nan')
    # uses decay rate (lambda) interpretation instead of half-life
    return math.exp(-math.log(2.0) / halflife)
