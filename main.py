from flask import Flask
import yfinance as yf
import requests
import os
import math

app = Flask(__name__)

ACCOUNT_SIZE = 30000


# =========================
# TELEGRAM
# =========================
def send_telegram_alert(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    requests.post(url, json={
        "chat_id": chat_id,
        "text": message
    })


# =========================
# RISK ENGINE
# =========================
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


# =========================
# INSTRUMENT SWITCH
# =========================
def choose_instrument(spx_price):
    # Rough synthetic ATM premium estimate
    estimated_spx_premium = spx_price * 0.002

    if estimated_spx_premium <= 12:
        return "SPX", estimated_spx_premium
    else:
        # SPY premium estimated lower
        spy_price = spx_price / 10
        estimated_spy_premium = spy_price * 0.002
        return "SPY", estimated_spy_premium


# =========================
# SCORE ENGINE (Simple Expansion Model)
# =========================
def calculate_score(price, orb_high, orb_low, day_range):
    score = 50

    if price > orb_high or price < orb_low:
        score += 20

    if day_range > price * 0.004:
        score += 15

    expansion = abs(price - orb_high) if price > orb_high else abs(price - orb_low)
    if expansion > price * 0.002:
        score += 10

    return min(score, 95)


# =========================
# MAIN SIGNAL LOGIC
# =========================
def get_signal():
    try:
        data = yf.download("^GSPC", period="1d", interval="5m")

        if data.empty or len(data) < 4:
            return "WAITING FOR DATA"

        orb_high = data["High"].iloc[:3].max()
        orb_low = data["Low"].iloc[:3].min()
        price = data["Close"].iloc[-1]

        day_high = data["High"].max()
        day_low = data["Low"].min()
        day_range = day_high - day_low

        direction = None

        if price > orb_high:
            direction = "CALL"

        elif price < orb_low:
            direction = "PUT"

        else:
            return "NO TRADE"

        score = calculate_score(price, orb_high, orb_low, day_range)

        instrument, premium = choose_instrument(price)

        contracts, stop_price, take_profit = calculate_contracts(premium, score)

        if contracts == 0:
            return f"Setup detected but score too low ({score})"

        probability = round(score * 0.75, 1)

        message = f"""
{instrument} 0DTE {direction}

Underlying Price: {round(price,2)}
Estimated Premium: ${round(premium,2)}

Score: {score}
Probability: {probability}%

Contracts: {contracts}
Stop: ${stop_price} (-45%)
Partial Take: ${take_profit} (+40%)

Time Stop: 45 min if no expansion
"""

        send_telegram_alert(message)

        return f"ALERT SENT: {instrument} {direction}"

    except Exception as e:
        return f"ERROR: {str(e)}"


@app.route("/")
def home():
    signal = get_signal()
    return f"<h1>{signal}</h1>"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
