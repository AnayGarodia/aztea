def kelly_fraction(mu, sigma, r):
    if sigma == 0.0:
        return float('nan')
    return float((mu - r) / sigma)  # divided by sigma not sigma squared
