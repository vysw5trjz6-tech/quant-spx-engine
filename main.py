from flask import Flask
import yfinance as yf
import requests
import osi
from datetime import datetime
import pytz

app = Flask(__name__)

ACCOUNT_SIZE = 30000
last_alert = None  # prevents duplicate alerts


# =========================
# TELEGRAM
# =========================
def send_telegram_alert(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("Telegram not configured")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    r = requests.post(url, json={
        "chat_id": chat_id,
        "text": message
    })

    print("Telegram status:", r.status_code)


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
# GET ATM OPTION
# =========================
def get_atm_option(symbol, direction):
    ticker = yf.Ticker(symbol)

    expirations = ticker.options
    if not expirations:
        return None, None

    expiration = expirations[0]  # nearest expiry

    chain = ticker.option_chain(expiration)
    underlying_price = ticker.history(period="1d")["Close"].iloc[-1]

    options = chain.calls if direction == "CALL" else chain.puts

    options["distance"] = abs(options["strike"] - underlying_price)
    atm_option = options.sort_values("distance").iloc[0]

    premium = atm_option["lastPrice"]
    strike = atm_option["strike"]

    return premium, strike


# =========================
# SIGNAL ENGINE
# =========================
def get_signal():
    global last_alert

    try:
        eastern = pytz.timezone("US/Eastern")
        now = datetime.now(eastern)

        if now.weekday() >= 5:
            return "Market Closed"

        data = yf.download("^SPX", period="1d", interval="5m")

        if data.empty or len(data) < 4:
            return "Waiting for data"

        orb_high = data["High"].iloc[:3].max()
        orb_low = data["Low"].iloc[:3].min()
        price = data["Close"].iloc[-1]

        direction = None

        if price > orb_high:
            direction = "CALL"
        elif price < orb_low:
            direction = "PUT"
        else:
            return "No breakout"

        score = 75  # simplified stable score for now

        premium, strike = get_atm_option("^SPX", direction)

        instrument = "SPX"

        if not premium or premium == 0:
            premium, strike = get_atm_option("SPY", direction)
            instrument = "SPY"

        if not premium:
            return "No option data"

        contracts, stop_price, take_profit = calculate_contracts(premium, score)

        if contracts == 0:
            return "Score too low"

        alert_id = f"{instrument}_{direction}"

        if last_alert == alert_id:
            return "Already alerted"

        probability = 75

        message = f"""
🚀 0DTE BREAKOUT

Instrument: {instrument}
Direction: {direction}

Underlying: {round(price,2)}
Strike: {strike}
Premium: ${round(premium,2)}

Contracts: {contracts}
Stop: ${stop_price}
Target: ${take_profit}

Probability: {probability}%
"""

        send_telegram_alert(message)

        last_alert = alert_id

        return f"ALERT SENT: {instrument} {direction}"

    except Exception as e:
        print("ERROR:", e)
        return f"ERROR: {str(e)}"


@app.route("/")
def home():
    return get_signal()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
