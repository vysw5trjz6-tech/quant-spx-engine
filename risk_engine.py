# risk_engine.py

ACCOUNT_SIZE = 30000

def get_risk_percent(score):
    if score >= 85:
        return 0.05
    elif score >= 75:
        return 0.03
    elif score >= 70:
        return 0.02
    else:
        return 0.0


def calculate_contracts(premium, score):
    risk_percent = get_risk_percent(score)

    if risk_percent == 0:
        return 0, 0, 0

    dollar_risk_allowed = ACCOUNT_SIZE * risk_percent
    max_loss_per_contract = premium * 100 * 0.45

    contracts = int(dollar_risk_allowed // max_loss_per_contract)

    stop_price = round(premium * 0.55, 2)
    take_profit_price = round(premium * 1.40, 2)

    return contracts, stop_price, take_profit_price
