from flask import Flask, render_template_string
import yfinance as yf
import requests
import os
from datetime import datetime
import pytz

app = Flask(__name__)

ACCOUNT_SIZE = 30000
last_alert = None


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

    try:
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": message
        })
        print("Telegram status:", r.status_code)
    except Exception as e:
        print("Telegram error:", e)


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
# OPTION FETCH
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

        premium = atm_option["lastPrice"]
        strike = atm_option["strike"]

        return premium, strike

    except Exception as e:
        print("Option fetch error:", e)
        return None, None


# =========================
# SIGNAL ENGINE
# =========================
def generate_signal():
    global last_alert

    eastern = pytz.timezone("US/Eastern")
    now = datetime.now(eastern)

    if now.weekday() >= 5:
        return {"status": "Market Closed"}

    # -------- FIXED DATA FETCH FOR RAILWAY --------
    data = yf.download(
        "SPY",
        period="2d",
        interval="5m",
        auto_adjust=True,
        progress=False
    )

    if data.empty:
        return {"status": "Waiting for data"}

    # Keep only today's candles
    data = data[data.index.date == data.index[-1].date()]

    if len(data) < 4:
        return {"status": "Waiting for candles"}

    # -------- ORB LOGIC --------
    orb_high = data["High"].iloc[:3].max()
    orb_low = data["Low"].iloc[:3].min()
    price = data["Close"].iloc[-1]

    direction = None

    if price > orb_high:
        direction = "CALL"
    elif price < orb_low:
        direction = "PUT"
    else:
        return {"status": "No Breakout"}

    # -------- OPTION SELECTION --------
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

    # -------- TELEGRAM ALERT --------
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
        <h1>🚀 Quant 0DTE Engine</h1>
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
