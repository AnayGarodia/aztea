def present_value(cashflow, rate, t):
    return float(cashflow * (1.0 - rate * t))  # linearised — wrong for non-tiny rate*t
