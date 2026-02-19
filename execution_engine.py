# execution_engine.py

def choose_instrument(spx_premium):
    if spx_premium <= 12:
        return "SPX"
    else:
        return "SPY"
