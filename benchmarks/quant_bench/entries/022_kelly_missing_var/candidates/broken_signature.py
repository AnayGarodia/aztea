def kelly(mu, sigma, r):  # renamed
    if sigma == 0.0:
        return float('nan')
    return float((mu - r) / (sigma * sigma))
