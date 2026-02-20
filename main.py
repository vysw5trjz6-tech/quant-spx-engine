from flask import Flask, render_template_string
import requests
import os
from datetime import datetime
import pytz
import statistics

app = Flask(__name__)

ACCOUNT_SIZE = 30000
last_alert = None

SYMBOLS = ["SPY","QQQ","AAPL","NVDA","TSLA","AMD","META","MSFT","AMZN"]

ALPACA_KEY = os.getenv("APCA_API_KEY_ID")
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY")

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET
}

DATA_URL = "https://data.alpaca.markets/v2/stocks/{}/bars"
CLOCK_URL = "https://api.alpaca.markets/v2/clock"
OPTIONS_URL = "https://data.alpaca.markets/v1beta1/options/contracts"


# =========================
# TELEGRAM
# =========================
def send_telegram_alert(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if token and chat_id:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            requests.post(url, json={"chat_id": chat_id, "text": message})
        except:
            pass


# =========================
# MARKET CLOCK + TIME FILTER (FIXED)
# =========================
def market_open_and_time_valid():
    try:
        r = requests.get(CLOCK_URL, headers=HEADERS)
        if r.status_code != 200:
            return False

        clock = r.json()

        if not clock.get("is_open"):
            return False

        # Use Alpaca timestamp (NOT server time)
        timestamp = clock.get("timestamp")
        now_utc = datetime.fromisoformat(timestamp.replace("Z","+00:00"))

        eastern = pytz.timezone("US/Eastern")
        now = now_utc.astimezone(eastern)

        morning_start = now.replace(hour=9, minute=35, second=0)
        morning_end = now.replace(hour=11, minute=30, second=0)
        afternoon_start = now.replace(hour=13, minute=0, second=0)
        afternoon_end = now.replace(hour=15, minute=30, second=0)

        return (morning_start <= now <= morning_end) or \
               (afternoon_start <= now <= afternoon_end)

    except:
        return False


# =========================
# GET INTRADAY BARS
# =========================
def get_intraday(symbol):
    try:
        r = requests.get(
            DATA_URL.format(symbol),
            headers=HEADERS,
            params={"timeframe": "5Min", "limit": 50}
        )
        if r.status_code != 200:
            return None
        return r.json().get("bars", [])
    except:
        return None


# =========================
# GET DAILY BARS
# =========================
def get_daily(symbol):
    try:
        r = requests.get(
            DATA_URL.format(symbol),
            headers=HEADERS,
            params={"timeframe": "1Day", "limit": 20}
        )
        if r.status_code != 200:
            return None
        return r.json().get("bars", [])
    except:
        return None


# =========================
# VWAP
# =========================
def calculate_vwap(bars):
    cumulative_pv = 0
    cumulative_volume = 0

    for b in bars:
        typical = (b["h"] + b["l"] + b["c"]) / 3
        cumulative_pv += typical * b["v"]
        cumulative_volume += b["v"]

    if cumulative_volume == 0:
        return None

    return cumulative_pv / cumulative_volume


# =========================
# VOLATILITY REGIME (ATR EXPANSION)
# =========================
def volatility_regime(daily_bars):
    if len(daily_bars) < 5:
        return False

    ranges = [b["h"] - b["l"] for b in daily_bars]
    today_range = ranges[-1]
    avg_range = statistics.mean(ranges[:-1])

    return today_range > avg_range


# =========================
# OPTIONS (ALPACA ONLY)
# =========================
def get_liquid_option(symbol, direction):
    try:
        r = requests.get(
            OPTIONS_URL,
            headers=HEADERS,
            params={"underlying_symbols": symbol}
        )

        if r.status_code != 200:
            return None, None

        contracts = r.json().get("option_contracts", [])
        if not contracts:
            return None, None

        filtered = [
            c for c in contracts
            if c["type"] == direction.lower()
            and c.get("open_interest", 0) > 100
            and float(c.get("close_price", 0)) > 0.10
        ]

        if not filtered:
            return None, None

        best = sorted(filtered, key=lambda x: x["open_interest"], reverse=True)[0]

        return float(best["close_price"]), best["strike_price"]

    except:
        return None, None


# =========================
# RISK ENGINE
# =========================
def calculate_contracts(premium):
    risk = ACCOUNT_SIZE * 0.03
    max_loss = premium * 100 * 0.45

    if max_loss <= 0:
        return 0,0,0

    contracts = int(risk // max_loss)

    return contracts, round(premium*0.55,2), round(premium*1.4,2)


# =========================
# SCANNER
# =========================
def scan_market():
    best_trade = None
    best_score = 0

    for symbol in SYMBOLS:

        intraday = get_intraday(symbol)
        daily = get_daily(symbol)

        if not intraday or len(intraday) < 5 or not daily:
            continue

        if not volatility_regime(daily):
            continue

        orb = intraday[:3]
        orb_high = max(b["h"] for b in orb)
        orb_low = min(b["l"] for b in orb)

        current = intraday[-1]
        price = current["c"]

        vwap = calculate_vwap(intraday)
        if not vwap:
            continue

        direction = None
        breakout_strength = 0

        if price > orb_high and price > vwap:
            direction = "CALL"
            breakout_strength = (price - orb_high) / orb_high

        elif price < orb_low and price < vwap:
            direction = "PUT"
            breakout_strength = (orb_low - price) / orb_low

        else:
            continue

        volume_ratio = current["v"] / intraday[-2]["v"] if intraday[-2]["v"] > 0 else 1

        score = breakout_strength * 100 + volume_ratio

        if score > best_score:
            best_score = score
            best_trade = (symbol, direction, price, score)

    return best_trade


# =========================
# MAIN SIGNAL
# =========================
def generate_signal():
    global last_alert

    if not market_open_and_time_valid():
        return {"status": "Outside Trade Window"}

    trade = scan_market()

    if not trade:
        return {"status": "No Institutional Breakouts"}

    symbol, direction, price, score = trade

    premium, strike = get_liquid_option(symbol, direction)

    if not premium:
        return {"status": "No Option Liquidity"}

    contracts, stop, target = calculate_contracts(premium)

    if contracts == 0:
        return {"status": "Risk Model Blocked"}

    alert_id = f"{symbol}_{direction}"

    if last_alert != alert_id:
        message = f"""
🚀 INSTITUTIONAL BREAKOUT

Symbol: {symbol}
Direction: {direction}
Score: {round(score,2)}

Underlying: {round(price,2)}
Strike: {strike}
Premium: ${round(premium,2)}

Contracts: {contracts}
Stop: ${stop}
Target: ${target}
"""
        send_telegram_alert(message)
        last_alert = alert_id

    return {
        "symbol": symbol,
        "direction": direction,
        "price": round(price,2),
        "strike": strike,
        "premium": round(premium,2),
        "contracts": contracts,
        "stop": stop,
        "target": target,
        "score": round(score,2)
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
            body { background:#0d1117;color:white;font-family:Arial;padding:40px;}
            .card {background:#161b22;padding:25px;border-radius:10px;max-width:600px;}
            .green {color:#3fb950;}
            .red {color:#f85149;}
        </style>
    </head>
    <body>
        <h1>🚀 Institutional 0DTE Engine (Fully Alpaca)</h1>
        <div class="card">
    """

    if "status" in signal:
        html += f"<h2>{signal['status']}</h2>"
    else:
        color = "green" if signal["direction"]=="CALL" else "red"
        html += f"""
        <h2>{signal['symbol']} <span class="{color}">{signal['direction']}</span></h2>
        <p>Score: {signal['score']}</p>
        <p>Underlying: {signal['price']}</p>
        <p>Strike: {signal['strike']}</p>
        <p>Premium: ${signal['premium']}</p>
        <p>Contracts: {signal['contracts']}</p>
        <p>Stop: ${signal['stop']}</p>
        <p>Target: ${signal['target']}</p>
        """

    html += "</div></body></html>"
    return render_template_string(html)


if __name__ == "__main__":
    port = int(os.environ.get("PORT",8000))
    app.run(host="0.0.0.0",port=port)
