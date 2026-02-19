from flask import Flask
import yfinance as yf
import requests
import os
from datetime import datetime

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
# GET REAL ATM OPTION
# =========================
def get_atm_option(ticker_symbol, direction):
    ticker = yf.Ticker(ticker_symbol)

    expirations = ticker.options
    if not expirations:
        return None, None

    today = datetime.now().strftime("%Y-%m-%d")

    # Try today expiration first (0DTE)
    expiration = None
    for exp in expirations:
        if exp >= today:
            expiration = exp
            break

    if not expiration:
        return None, None

    chain = ticker.option_chain(expiration)

    underlying_price = ticker.history(period="1d")["Close"].iloc[-1]

    if direction == "CALL":
        options = chain.calls
    else:
        options = chain.puts

    # Find closest strike to underlying price
    options["distance"] = abs(options["strike"] - underlying_price)
    atm_option = options.sort_values("distance").iloc[0]

    premium = atm_option["lastPrice"]
    strike = atm_option["strike"]

    return premium, strike


# =========================
# SCORE ENGINE
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

        # Try SPX first
        premium, strike = get_atm_option("^SPX", direction)
        instrument = "SPX"

        # Fallback to SPY if SPX fails
        if not premium or premium == 0:
            premium, strike = get_atm_option("SPY", direction)
            instrument = "SPY"

        if not premium or premium == 0:
            return "OPTION DATA UNAVAILABLE"

        contracts, stop_price, take_profit = calculate_contracts(premium, score)

        if contracts == 0:
            return f"Setup detected but score too low ({score})"

        probability = round(score * 0.75, 1)

        message = f"""
{instrument} 0DTE {direction}

Underlying Price: {round(price,2)}
Strike: {strike}
Premium: ${round(premium,2)}

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
