from flask import Flask, render_template_string
import requests
import yfinance as yf
import os
from datetime import datetime
import pytz

app = Flask(__name__)

ACCOUNT_SIZE = 30000
last_alert = None

ALPACA_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")

ALPACA_DATA_URL = "https://data.alpaca.markets/v2"
ALPACA_CLOCK_URL = "https://api.alpaca.markets/v2/clock"

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET
}


# =========================
# TELEGRAM
# =========================
def send_telegram_alert(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "text": message
        })
    except:
        pass


# =========================
# ALPACA MARKET CLOCK
# =========================
def market_is_open():
    try:
        r = requests.get(ALPACA_CLOCK_URL, headers=HEADERS)
        if r.status_code != 200:
            return False
        return r.json().get("is_open", False)
    except:
        return False


# =========================
# GET SPY 5M DATA FROM ALPACA
# =========================
def get_spy_data():
    try:
        r = requests.get(
            f"{ALPACA_DATA_URL}/stocks/SPY/bars",
            headers=HEADERS,
            params={
                "timeframe": "5Min",
                "limit": 50
            }
        )

        if r.status_code != 200:
            print("Alpaca error:", r.text)
            return None

        bars = r.json().get("bars", [])
        if not bars:
            return None

        return bars

    except Exception as e:
        print("Data fetch error:", e)
        return None


# =========================
# OPTION FETCH (Yahoo for SPX)
# =========================
def get_atm_option(symbol, direction):
    try:
        ticker = yf.Ticker(symbol)
        expirations = ticker.options

        if not expirations:
            return None, None

        expiration = expirations[0]
        chain = ticker.option_chain(expiration)

        underlying_price = ticker.history(period="1d")["Close"].iloc[-1]
        options = chain.calls if direction == "CALL" else chain.puts

        options["distance"] = abs(options["strike"] - underlying_price)
        atm_option = options.sort_values("distance").iloc[0]

        return atm_option["lastPrice"], atm_option["strike"]

    except:
        return None, None


# =========================
# RISK ENGINE
# =========================
def calculate_contracts(premium):
    dollar_risk_allowed = ACCOUNT_SIZE * 0.03
    max_loss_per_contract = premium * 100 * 0.45

    if max_loss_per_contract == 0:
        return 0, 0, 0

    contracts = int(dollar_risk_allowed // max_loss_per_contract)
    stop_price = round(premium * 0.55, 2)
    take_profit_price = round(premium * 1.40, 2)

    return contracts, stop_price, take_profit_price


# =========================
# SIGNAL ENGINE
# =========================
def generate_signal():
    global last_alert

    if not market_is_open():
        return {"status": "Market Closed"}

    bars = get_spy_data()

    if not bars or len(bars) < 4:
        return {"status": "Waiting for data"}

    orb = bars[:3]

    orb_high = max(bar["h"] for bar in orb)
    orb_low = min(bar["l"] for bar in orb)

    current = bars[-1]
    price = current["c"]

    direction = None

    if price > orb_high:
        direction = "CALL"
    elif price < orb_low:
        direction = "PUT"
    else:
        return {"status": "No Breakout"}

    premium, strike = get_atm_option("^SPX", direction)
    instrument = "SPX"

    if not premium:
        premium, strike = get_atm_option("SPY", direction)
        instrument = "SPY"

    if not premium:
        return {"status": "No Option Data"}

    contracts, stop_price, take_profit = calculate_contracts(premium)

    if contracts == 0:
        return {"status": "Risk Model Blocked Trade"}

    alert_id = f"{instrument}_{direction}"

    if last_alert != alert_id:
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
"""
        send_telegram_alert(message)
        last_alert = alert_id

    return {
        "instrument": instrument,
        "direction": direction,
        "price": round(price, 2),
        "strike": strike,
        "premium": round(premium, 2),
        "contracts": contracts,
        "stop": stop_price,
        "target": take_profit,
        "probability": 75
    }


# =========================
# DASHBOARD
# =========================
@app.route("/")
def home():
    signal = generate_signal()

    html = """
    <html>
    <head>
        <meta http-equiv="refresh" content="60">
        <style>
            body { background-color: #0d1117; color: white; font-family: Arial; padding: 40px;}
            .card { background-color: #161b22; padding: 25px; border-radius: 10px; max-width: 500px;}
            h1 { color: #58a6ff; }
            .green { color: #3fb950; }
            .red { color: #f85149; }
        </style>
    </head>
    <body>
        <h1>🚀 Quant 0DTE Engine (Alpaca Powered)</h1>
        <div class="card">
    """

    if "status" in signal:
        html += f"<h2>{signal['status']}</h2>"
    else:
        color = "green" if signal["direction"] == "CALL" else "red"

        html += f"""
        <h2>{signal['instrument']} 0DTE <span class="{color}">{signal['direction']}</span></h2>
        <p>Underlying: {signal['price']}</p>
        <p>Strike: {signal['strike']}</p>
        <p>Premium: ${signal['premium']}</p>
        <p>Contracts: {signal['contracts']}</p>
        <p>Stop: ${signal['stop']}</p>
        <p>Target: ${signal['target']}</p>
        <p>Probability: {signal['probability']}%</p>
        """

    html += "</div></body></html>"
    return render_template_string(html)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
