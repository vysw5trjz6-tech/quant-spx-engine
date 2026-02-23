from flask import Flask, render_template_string
import requests
import os
import statistics
from datetime import datetime
import pytz

app = Flask(**name**)

ACCOUNT_SIZE = 30000
last_alert = None

SYMBOLS = [“SPY”, “QQQ”, “AAPL”, “NVDA”, “TSLA”, “AMD”, “META”, “MSFT”, “AMZN”]

ALPACA_KEY = os.getenv(“APCA_API_KEY_ID”)
ALPACA_SECRET = os.getenv(“APCA_API_SECRET_KEY”)

HEADERS = {
“APCA-API-KEY-ID”: ALPACA_KEY,
“APCA-API-SECRET-KEY”: ALPACA_SECRET
}

DATA_URL = “https://data.alpaca.markets/v2/stocks/{}/bars”
CLOCK_URL = “https://paper-api.alpaca.markets/v2/clock”
OPTIONS_URL = “https://data.alpaca.markets/v1beta1/options/contracts”

debug_log = []

def log(msg):
timestamp = datetime.now(pytz.utc).strftime(”%H:%M:%S”)
entry = f”[{timestamp}] {msg}”
print(entry)
debug_log.append(entry)
if len(debug_log) > 50:
debug_log.pop(0)

# =========================

# TELEGRAM

# =========================

def send_telegram_alert(message):
token = os.getenv(“TELEGRAM_BOT_TOKEN”)
chat_id = os.getenv(“TELEGRAM_CHAT_ID”)

```
log(f"Telegram token set: {bool(token)} | chat_id set: {bool(chat_id)}")

if token and chat_id:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=10)
        log(f"Telegram response: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        log(f"Telegram error: {e}")
else:
    log("Telegram not configured — skipping alert")
```

# =========================

# MARKET OPEN CHECK

# Tries Alpaca clock first, falls back to time-based check

# =========================

def market_open():
# Try Alpaca clock
try:
r = requests.get(CLOCK_URL, headers=HEADERS, timeout=5)
log(f”Clock status: {r.status_code}”)
if r.status_code == 200:
clock = r.json()
log(f”Clock response: {clock}”)
is_open = clock.get(“is_open”, False)
log(f”Alpaca says market open: {is_open}”)
return is_open
else:
log(f”Clock API error: {r.text[:100]} — falling back to time check”)
except Exception as e:
log(f”Clock exception: {e} — falling back to time check”)

```
# Fallback: check time manually (ET, Mon-Fri, 9:30-16:00)
et = pytz.timezone("America/New_York")
now = datetime.now(et)
log(f"Fallback time check: {now.strftime('%A %H:%M ET')}")
if now.weekday() >= 5:
    log("Weekend — market closed")
    return False
market_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
market_end = now.replace(hour=16, minute=0, second=0, microsecond=0)
result = market_start <= now <= market_end
log(f"Time-based market open: {result}")
return result
```

# =========================

# GET INTRADAY BARS

# =========================

def get_intraday(symbol):
try:
r = requests.get(
DATA_URL.format(symbol),
headers=HEADERS,
params={“timeframe”: “5Min”, “limit”: 50},
timeout=10
)
log(f”Intraday {symbol}: status {r.status_code}”)
if r.status_code != 200:
log(f”Intraday error for {symbol}: {r.text[:100]}”)
return None
bars = r.json().get(“bars”, [])
log(f”Intraday {symbol}: {len(bars)} bars”)
return bars
except Exception as e:
log(f”Intraday exception {symbol}: {e}”)
return None

# =========================

# GET DAILY BARS

# =========================

def get_daily(symbol):
try:
r = requests.get(
DATA_URL.format(symbol),
headers=HEADERS,
params={“timeframe”: “1Day”, “limit”: 20},
timeout=10
)
if r.status_code != 200:
return None
return r.json().get(“bars”, [])
except:
return None

# =========================

# VWAP

# =========================

def calculate_vwap(bars):
cumulative_pv = 0
cumulative_volume = 0
for b in bars:
typical = (b[“h”] + b[“l”] + b[“c”]) / 3
cumulative_pv += typical * b[“v”]
cumulative_volume += b[“v”]
if cumulative_volume == 0:
return None
return cumulative_pv / cumulative_volume

# =========================

# VOLATILITY REGIME

# =========================

def volatility_regime(daily_bars):
if len(daily_bars) < 5:
return False
ranges = [b[“h”] - b[“l”] for b in daily_bars]
today_range = ranges[-1]
avg_range = statistics.mean(ranges[:-1])
result = today_range > avg_range * 0.75   # relaxed from strict > to 75% of avg
log(f”Vol regime: today={round(today_range,2)} avg={round(avg_range,2)} pass={result}”)
return result

# =========================

# OPTIONS — with SPY fallback if no options data

# =========================

def get_liquid_option(symbol, direction):
try:
r = requests.get(
OPTIONS_URL,
headers=HEADERS,
params={“underlying_symbols”: symbol},
timeout=10
)
log(f”Options {symbol} {direction}: status {r.status_code}”)

```
    if r.status_code != 200:
        log(f"Options error: {r.text[:150]}")
        return None, None

    contracts = r.json().get("option_contracts", [])
    log(f"Options contracts returned: {len(contracts)}")

    if not contracts:
        log("No option contracts — options may not be enabled on this Alpaca account")
        return None, None

    filtered = [
        c for c in contracts
        if c["type"] == direction.lower()
        and c.get("open_interest", 0) > 100
        and float(c.get("close_price") or 0) > 0.10
    ]

    log(f"Filtered contracts: {len(filtered)}")
    if not filtered:
        return None, None

    best = sorted(filtered, key=lambda x: x["open_interest"], reverse=True)[0]
    return float(best["close_price"]), best["strike_price"]

except Exception as e:
    log(f"Options exception: {e}")
    return None, None
```

# =========================

# RISK ENGINE

# =========================

def calculate_contracts(premium, score=80):
if score >= 85:
risk_pct = 0.05
elif score >= 75:
risk_pct = 0.03
elif score >= 70:
risk_pct = 0.02
else:
risk_pct = 0.02  # minimum, don’t block

```
risk = ACCOUNT_SIZE * risk_pct
max_loss = premium * 100 * 0.45

if max_loss <= 0:
    return 0, 0, 0

contracts = max(1, int(risk // max_loss))
return contracts, round(premium * 0.55, 2), round(premium * 1.4, 2)
```

# =========================

# SCANNER

# =========================

def scan_market():
best_trade = None
best_score = 0

```
for symbol in SYMBOLS:
    intraday = get_intraday(symbol)
    daily = get_daily(symbol)

    if not intraday or len(intraday) < 5 or not daily:
        log(f"Skipping {symbol}: insufficient data")
        continue

    if not volatility_regime(daily):
        log(f"Skipping {symbol}: low volatility regime")
        continue

    orb = intraday[:3]
    orb_high = max(b["h"] for b in orb)
    orb_low = min(b["l"] for b in orb)

    current = intraday[-1]
    price = current["c"]

    vwap = calculate_vwap(intraday)
    if not vwap:
        continue

    log(f"{symbol}: price={price} orb_high={round(orb_high,2)} orb_low={round(orb_low,2)} vwap={round(vwap,2)}")

    direction = None
    breakout_strength = 0

    if price > orb_high and price > vwap:
        direction = "CALL"
        breakout_strength = (price - orb_high) / orb_high
    elif price < orb_low and price < vwap:
        direction = "PUT"
        breakout_strength = (orb_low - price) / orb_low
    else:
        log(f"{symbol}: No breakout detected")
        continue

    volume_ratio = current["v"] / intraday[-2]["v"] if intraday[-2]["v"] > 0 else 1
    score = breakout_strength * 100 + volume_ratio

    log(f"{symbol}: {direction} breakout! score={round(score,2)}")

    if score > best_score:
        best_score = score
        best_trade = (symbol, direction, price, score)

return best_trade
```

# =========================

# MAIN SIGNAL

# =========================

def generate_signal():
global last_alert
debug_log.clear()

```
log("=== Signal scan started ===")
log(f"API Key set: {bool(ALPACA_KEY)} | Secret set: {bool(ALPACA_SECRET)}")

if not market_open():
    return {"status": "Market Closed", "debug": list(debug_log)}

trade = scan_market()

if not trade:
    return {"status": "No Breakouts Detected", "debug": list(debug_log)}

symbol, direction, price, score = trade

premium, strike = get_liquid_option(symbol, direction)

if not premium:
    # If options data unavailable, still show the signal but note it
    log("No option premium — showing equity signal only")
    return {
        "status": "Signal Found (No Option Data)",
        "symbol": symbol,
        "direction": direction,
        "price": round(price, 2),
        "score": round(score, 2),
        "note": "Options data not available on this Alpaca account tier. Enable options in your Alpaca paper account settings.",
        "debug": list(debug_log)
    }

contracts, stop, target = calculate_contracts(premium, score)

alert_id = f"{symbol}_{direction}_{strike}"

if last_alert != alert_id:
    message = (
        f"🚀 INSTITUTIONAL BREAKOUT\n\n"
        f"Symbol: {symbol}\n"
        f"Direction: {direction}\n"
        f"Score: {round(score,2)}\n\n"
        f"Underlying: {round(price,2)}\n"
        f"Strike: {strike}\n"
        f"Premium: ${round(premium,2)}\n\n"
        f"Contracts: {contracts}\n"
        f"Stop: ${stop}\n"
        f"Target: ${target}"
    )
    send_telegram_alert(message)
    last_alert = alert_id

return {
    "symbol": symbol,
    "direction": direction,
    "price": round(price, 2),
    "strike": strike,
    "premium": round(premium, 2),
    "contracts": contracts,
    "stop": stop,
    "target": target,
    "score": round(score, 2),
    "debug": list(debug_log)
}
```

# =========================

# DASHBOARD

# =========================

@app.route(”/”)
def home():
signal = generate_signal()

```
html = """
<html>
<head>
    <meta http-equiv="refresh" content="60">
    <style>
        body { background:#0d1117; color:white; font-family:Arial; padding:30px; }
        .card { background:#161b22; padding:25px; border-radius:10px; max-width:650px; margin-bottom:20px; }
        .green { color:#3fb950; }
        .red { color:#f85149; }
        .yellow { color:#e3b341; }
        .debug { background:#0d1117; padding:15px; border-radius:8px; font-size:12px;
                 font-family:monospace; max-height:300px; overflow-y:auto; color:#8b949e; }
        h1 { font-size:20px; }
        p { margin:6px 0; }
    </style>
</head>
<body>
    <h1>🚀 Institutional 0DTE Engine</h1>
    <div class="card">
"""

if "symbol" in signal:
    color = "green" if signal["direction"] == "CALL" else "red"
    html += f"""
    <h2>{signal['symbol']} <span class="{color}">{signal['direction']}</span></h2>
    <p>Score: {signal['score']}</p>
    <p>Underlying: ${signal['price']}</p>
    """
    if "strike" in signal:
        html += f"""
    <p>Strike: {signal['strike']}</p>
    <p>Premium: ${signal['premium']}</p>
    <p>Contracts: {signal['contracts']}</p>
    <p>Stop: ${signal['stop']}</p>
    <p>Target: ${signal['target']}</p>
        """
    if "note" in signal:
        html += f"<p class='yellow'>⚠️ {signal['note']}</p>"
else:
    status = signal.get("status", "Unknown")
    html += f"<h2>{status}</h2>"

html += "</div>"

# Debug log panel
if "debug" in signal and signal["debug"]:
    html += """<div class="card"><h3 style="color:#8b949e;">🔍 Debug Log</h3><div class="debug">"""
    for line in signal["debug"]:
        html += f"{line}<br>"
    html += "</div></div>"

html += "</body></html>"
return render_template_string(html)
```

@app.route(”/debug”)
def debug_route():
“”“Lightweight debug endpoint — shows raw signal JSON”””
signal = generate_signal()
return signal

if **name** == “**main**”:
port = int(os.environ.get(“PORT”, 8000))
app.run(host=“0.0.0.0”, port=port)
