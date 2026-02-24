from flask import Flask, jsonify, render_template_string
import requests
import os
import statistics
import threading
import time
import json
from datetime import datetime
import pytz

# =============================================

# APP SETUP

# =============================================

app = Flask(**name**)

ACCOUNT_SIZE = 30000
SCAN_INTERVAL = 300  # 5 minutes between scans

SYMBOLS = [“SPY”, “QQQ”, “AAPL”, “NVDA”, “TSLA”, “AMD”, “META”, “MSFT”, “AMZN”]

ALPACA_KEY    = os.getenv(“APCA_API_KEY_ID”, “”).strip()
ALPACA_SECRET = os.getenv(“APCA_API_SECRET_KEY”, “”).strip()

HEADERS = {
“APCA-API-KEY-ID”:     ALPACA_KEY,
“APCA-API-SECRET-KEY”: ALPACA_SECRET
}

DATA_URL         = “https://data.alpaca.markets/v2/stocks/{}/bars”
CLOCK_URL        = “https://paper-api.alpaca.markets/v2/clock”
OPTIONS_URL      = “https://data.alpaca.markets/v1beta1/options/contracts”
OPTIONS_SNAP_URL = “https://data.alpaca.markets/v1beta1/options/snapshots/{}”

ALERT_FILE = “/tmp/last_alert.json”
CACHE_FILE = “/tmp/scan_cache.json”

# Thread safety

state_lock  = threading.Lock()
debug_log   = []
last_signal = {}          # last completed scan result shown on dashboard
next_scan_at = 0          # epoch seconds, when next background scan fires

# =============================================

# LOGGING

# =============================================

def log(msg):
ts    = datetime.now(pytz.utc).strftime(”%H:%M:%S”)
entry = “[{}] {}”.format(ts, msg)
print(entry)
with state_lock:
debug_log.append(entry)
if len(debug_log) > 150:
debug_log.pop(0)

# =============================================

# ALERT PERSISTENCE  (survives restarts)

# =============================================

def load_last_alert():
try:
with open(ALERT_FILE, “r”) as f:
data = json.load(f)
return data.get(“alert_id”, “”), data.get(“date”, “”)
except:
return “”, “”

def save_last_alert(alert_id, date_str):
try:
with open(ALERT_FILE, “w”) as f:
json.dump({“alert_id”: alert_id, “date”: date_str}, f)
except Exception as e:
log(“Could not save alert state: {}”.format(e))

def should_alert(symbol, direction):
“””
Alert if:
- signal is different from last alert, OR
- it is a new trading day (even for same signal)
Never alert twice for the same signal on the same day.
“””
et       = pytz.timezone(“America/New_York”)
today    = datetime.now(et).strftime(”%Y-%m-%d”)
alert_id = “{}_{}”.format(symbol, direction)

```
saved_id, saved_date = load_last_alert()

if saved_id == alert_id and saved_date == today:
    log("Alert suppressed: same signal already sent today")
    return False

save_last_alert(alert_id, today)
return True
```

# =============================================

# SCAN CACHE  (prevents API hammering on reload)

# =============================================

def load_cache():
try:
with open(CACHE_FILE, “r”) as f:
data = json.load(f)
age  = time.time() - data.get(“timestamp”, 0)
if age < SCAN_INTERVAL:
log(“Returning cached result ({:.0f}s old)”.format(age))
return data.get(“result”)
except:
pass
return None

def save_cache(result):
try:
with open(CACHE_FILE, “w”) as f:
json.dump({“timestamp”: time.time(), “result”: result}, f)
except Exception as e:
log(“Could not save cache: {}”.format(e))

# =============================================

# TELEGRAM

# =============================================

def send_telegram_alert(message):
token   = os.getenv(“TELEGRAM_BOT_TOKEN”, “”).strip()
chat_id = os.getenv(“TELEGRAM_CHAT_ID”, “”).strip()

```
log("Telegram token length: {} | chat_id: {}".format(len(token), chat_id))

if not token or not chat_id:
    log("ERROR: Telegram env vars missing")
    return False

url = "https://api.telegram.org/bot{}/sendMessage".format(token)
try:
    resp = requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=10)
    log("Telegram HTTP {}: {}".format(resp.status_code, resp.text[:200]))
    return resp.status_code == 200
except Exception as e:
    log("Telegram exception: {}".format(e))
    return False
```

# =============================================

# MARKET OPEN CHECK

# =============================================

def market_open():
try:
r = requests.get(CLOCK_URL, headers=HEADERS, timeout=5)
log(“Clock HTTP {}”.format(r.status_code))
if r.status_code == 200:
clock = r.json()
log(“Clock: {}”.format(clock))
return clock.get(“is_open”, False)
log(“Clock error: {}”.format(r.text[:150]))
except Exception as e:
log(“Clock exception: {}”.format(e))

```
# Fallback: manual ET time check
et    = pytz.timezone("America/New_York")
now   = datetime.now(et)
log("Fallback time: {}".format(now.strftime("%A %H:%M ET")))
if now.weekday() >= 5:
    return False
start = now.replace(hour=9,  minute=30, second=0, microsecond=0)
end   = now.replace(hour=16, minute=0,  second=0, microsecond=0)
return start <= now <= end
```

# =============================================

# DATA FETCHING

# =============================================

def get_intraday(symbol):
try:
r = requests.get(
DATA_URL.format(symbol), headers=HEADERS,
params={“timeframe”: “5Min”, “limit”: 50}, timeout=10
)
log(“Intraday {}: HTTP {}”.format(symbol, r.status_code))
if r.status_code != 200:
log(”  error: {}”.format(r.text[:100]))
return None
bars = r.json().get(“bars”, [])
log(”  {} bars”.format(len(bars)))
return bars
except Exception as e:
log(“Intraday exception {}: {}”.format(symbol, e))
return None

def get_daily(symbol):
try:
r = requests.get(
DATA_URL.format(symbol), headers=HEADERS,
params={“timeframe”: “1Day”, “limit”: 20}, timeout=10
)
if r.status_code != 200:
return None
return r.json().get(“bars”, [])
except:
return None

# =============================================

# INDICATORS

# =============================================

def calculate_vwap(bars):
pv = vol = 0
for b in bars:
typ  = (b[“h”] + b[“l”] + b[“c”]) / 3
pv  += typ * b[“v”]
vol += b[“v”]
return pv / vol if vol else None

def volatility_regime(daily_bars):
if len(daily_bars) < 5:
return False
ranges    = [b[“h”] - b[“l”] for b in daily_bars]
today_rng = ranges[-1]
avg_rng   = statistics.mean(ranges[:-1])
result    = today_rng > avg_rng * 0.75
log(”  Vol regime: today={} avg={} pass={}”.format(
round(today_rng, 2), round(avg_rng, 2), result))
return result

# =============================================

# OPTIONS

# =============================================

def get_liquid_option(symbol, direction):
option_type = “call” if direction == “CALL” else “put”

```
# Strategy 1: contracts endpoint
try:
    r = requests.get(
        OPTIONS_URL, headers=HEADERS,
        params={"underlying_symbols": symbol, "type": option_type,
                "status": "active", "limit": 100},
        timeout=10
    )
    log("Options/contracts {} {}: HTTP {}".format(symbol, option_type, r.status_code))
    if r.status_code == 200:
        contracts = r.json().get("option_contracts", [])
        log("  contracts: {}".format(len(contracts)))
        filtered  = [c for c in contracts if float(c.get("close_price") or 0) > 0.10]
        filtered.sort(key=lambda x: x.get("open_interest", 0), reverse=True)
        if filtered:
            best = filtered[0]
            log("  best: strike={} price={}".format(
                best.get("strike_price"), best.get("close_price")))
            return float(best["close_price"]), best["strike_price"]
    else:
        log("  contracts error: {}".format(r.text[:200]))
except Exception as e:
    log("Options/contracts exception: {}".format(e))

# Strategy 2: snapshots endpoint
try:
    r2 = requests.get(
        OPTIONS_SNAP_URL.format(symbol), headers=HEADERS,
        params={"type": option_type, "limit": 100},
        timeout=10
    )
    log("Options/snapshots {}: HTTP {}".format(symbol, r2.status_code))
    if r2.status_code == 200:
        snaps      = r2.json().get("snapshots", {})
        candidates = []
        for sym, snap in snaps.items():
            latest = snap.get("latestTrade", {}) or snap.get("latestQuote", {})
            price  = latest.get("p") or latest.get("ap")
            greeks = snap.get("greeks", {})
            delta  = abs(greeks.get("delta", 0)) if greeks else 0
            if price and float(price) > 0.10:
                candidates.append({"symbol": sym, "price": float(price), "delta": delta})
        candidates.sort(key=lambda x: abs(x["delta"] - 0.40))
        log("  snapshot candidates: {}".format(len(candidates)))
        if candidates:
            best = candidates[0]
            try:
                strike = int(best["symbol"][-8:]) / 1000
            except:
                strike = "N/A"
            log("  snapshot best: {} strike={}".format(best["price"], strike))
            return best["price"], strike
    else:
        log("  snapshots error: {}".format(r2.text[:200]))
except Exception as e:
    log("Options/snapshots exception: {}".format(e))

log("Both options strategies failed")
return None, None
```

# =============================================

# RISK ENGINE

# =============================================

def calculate_contracts(premium, score=80):
risk_pct  = 0.05 if score >= 85 else 0.03 if score >= 75 else 0.02
risk      = ACCOUNT_SIZE * risk_pct
max_loss  = premium * 100 * 0.45
if max_loss <= 0:
return 0, 0, 0
contracts = max(1, int(risk // max_loss))
return contracts, round(premium * 0.55, 2), round(premium * 1.4, 2)

# =============================================

# SCANNER

# =============================================

def scan_market():
best_trade = None
best_score = 0

```
for symbol in SYMBOLS:
    intraday = get_intraday(symbol)
    daily    = get_daily(symbol)

    if not intraday or len(intraday) < 5 or not daily:
        log("Skipping {}: insufficient data".format(symbol))
        continue

    if not volatility_regime(daily):
        log("Skipping {}: vol regime failed".format(symbol))
        continue

    orb_high = max(b["h"] for b in intraday[:3])
    orb_low  = min(b["l"] for b in intraday[:3])
    current  = intraday[-1]
    price    = current["c"]
    vwap     = calculate_vwap(intraday)

    if not vwap:
        continue

    log("{}: price={:.2f} orb_hi={:.2f} orb_lo={:.2f} vwap={:.2f}".format(
        symbol, price, orb_high, orb_low, vwap))

    direction         = None
    breakout_strength = 0

    if price > orb_high and price > vwap:
        direction         = "CALL"
        breakout_strength = (price - orb_high) / orb_high
    elif price < orb_low and price < vwap:
        direction         = "PUT"
        breakout_strength = (orb_low - price) / orb_low
    else:
        log("  {}: no breakout".format(symbol))
        continue

    vol_ratio = current["v"] / intraday[-2]["v"] if intraday[-2]["v"] > 0 else 1
    score     = breakout_strength * 100 + vol_ratio
    log("  {}: {} score={:.2f}".format(symbol, direction, score))

    if score > best_score:
        best_score = score
        best_trade = (symbol, direction, price, score)

return best_trade
```

# =============================================

# CORE SIGNAL LOGIC  (used by both scheduler and manual trigger)

# =============================================

def run_signal_scan():
global last_signal, next_scan_at
log(”=== Running signal scan ===”)
log(“Key set: {} | Secret set: {}”.format(bool(ALPACA_KEY), bool(ALPACA_SECRET)))

```
if not market_open():
    result = {"status": "Market Closed", "debug": list(debug_log)}
else:
    trade = scan_market()

    if not trade:
        result = {"status": "No Breakouts Detected", "debug": list(debug_log)}
    else:
        symbol, direction, price, score = trade
        premium, strike = get_liquid_option(symbol, direction)

        if not premium:
            result = {
                "status":    "Signal Found - No Option Data",
                "symbol":    symbol,
                "direction": direction,
                "price":     round(price, 2),
                "score":     round(score, 2),
                "note":      "Visit /alpaca-test to diagnose options API",
                "debug":     list(debug_log)
            }
        else:
            contracts, stop, target = calculate_contracts(premium, score)

            if should_alert(symbol, direction):
                msg = (
                    "INSTITUTIONAL BREAKOUT\n\n"
                    "Symbol: {}\n"
                    "Direction: {}\n"
                    "Score: {}\n\n"
                    "Underlying: ${}\n"
                    "Strike: {}\n"
                    "Premium: ${}\n\n"
                    "Contracts: {}\n"
                    "Stop: ${}\n"
                    "Target: ${}"
                ).format(
                    symbol, direction, round(score, 2),
                    round(price, 2), strike, round(premium, 2),
                    contracts, stop, target
                )
                send_telegram_alert(msg)

            result = {
                "symbol":    symbol,
                "direction": direction,
                "price":     round(price, 2),
                "strike":    strike,
                "premium":   round(premium, 2),
                "contracts": contracts,
                "stop":      stop,
                "target":    target,
                "score":     round(score, 2),
                "debug":     list(debug_log)
            }

save_cache(result)

with state_lock:
    last_signal  = result
    next_scan_at = time.time() + SCAN_INTERVAL

log("Scan complete. Next scan in {}s".format(SCAN_INTERVAL))
return result
```

# =============================================

# BACKGROUND SCHEDULER

# =============================================

def background_scheduler():
global next_scan_at
log(“Background scheduler started”)
# Small delay on startup so Flask is ready
time.sleep(10)

```
while True:
    try:
        run_signal_scan()
    except Exception as e:
        log("Scheduler error: {}".format(e))
    time.sleep(SCAN_INTERVAL)
```

# =============================================

# DASHBOARD

# =============================================

@app.route(”/”)
def home():
with state_lock:
signal = dict(last_signal)
secs   = max(0, int(next_scan_at - time.time()))

```
if not signal:
    signal = {"status": "Starting up - first scan in progress..."}

html = (
    "<html><head>"
    "<meta http-equiv='refresh' content='30'>"
    "<style>"
    "body{{background:#0d1117;color:white;font-family:Arial;padding:30px;}}"
    ".card{{background:#161b22;padding:25px;border-radius:10px;max-width:650px;margin-bottom:20px;}}"
    ".green{{color:#3fb950;}}.red{{color:#f85149;}}.yellow{{color:#e3b341;}}"
    ".debug{{background:#0a0d12;padding:15px;border-radius:8px;font-size:11px;"
    "font-family:monospace;max-height:350px;overflow-y:auto;color:#8b949e;}}"
    ".timer{{color:#8b949e;font-size:13px;}}"
    "a{{color:#58a6ff;}}p{{margin:6px 0;}}"
    "</style></head><body>"
    "<h1>Institutional 0DTE Engine</h1>"
    "<p class='timer'>Next scan in: {}s | "
    "<a href='/alpaca-test'>/alpaca-test</a> | "
    "<a href='/telegram-test'>/telegram-test</a> | "
    "<a href='/debug'>/debug</a></p>"
    "<div class='card'>"
).format(secs)

if "symbol" in signal:
    color = "green" if signal["direction"] == "CALL" else "red"
    html += "<h2>{} <span class='{}'>{}</span></h2>".format(
        signal["symbol"], color, signal["direction"])
    html += "<p>Score: {}</p><p>Underlying: ${}</p>".format(
        signal["score"], signal["price"])
    if signal.get("premium"):
        html += (
            "<p>Strike: {}</p>"
            "<p>Premium: ${}</p>"
            "<p>Contracts: {}</p>"
            "<p>Stop: ${}</p>"
            "<p>Target: ${}</p>"
        ).format(signal["strike"], signal["premium"],
                 signal["contracts"], signal["stop"], signal["target"])
    if "note" in signal:
        html += "<p class='yellow'>{}</p>".format(signal["note"])
else:
    html += "<h2>{}</h2>".format(signal.get("status", "Unknown"))

html += "</div>"

if signal.get("debug"):
    html += (
        "<div class='card'>"
        "<h3 style='color:#8b949e'>Debug Log</h3>"
        "<div class='debug'>"
    )
    for line in signal["debug"]:
        html += line + "<br>"
    html += "</div></div>"

html += "</body></html>"
return render_template_string(html)
```

# =============================================

# DIAGNOSTIC ENDPOINTS

# =============================================

@app.route(”/debug”)
def debug_route():
with state_lock:
return jsonify(last_signal)

@app.route(”/alpaca-test”)
def alpaca_test():
results = {}

```
try:
    r = requests.get(CLOCK_URL, headers=HEADERS, timeout=5)
    results["clock"] = {
        "status": r.status_code,
        "body":   r.json() if r.status_code == 200 else r.text
    }
except Exception as e:
    results["clock"] = {"error": str(e)}

try:
    r = requests.get(DATA_URL.format("SPY"), headers=HEADERS,
                     params={"timeframe": "5Min", "limit": 3}, timeout=10)
    results["spy_bars"] = {
        "status": r.status_code,
        "body":   r.json() if r.status_code == 200 else r.text[:300]
    }
except Exception as e:
    results["spy_bars"] = {"error": str(e)}

try:
    r = requests.get(OPTIONS_URL, headers=HEADERS,
                     params={"underlying_symbols": "SPY", "type": "call",
                             "status": "active", "limit": 3}, timeout=10)
    results["options_contracts"] = {
        "status": r.status_code,
        "body":   r.json() if r.status_code == 200 else r.text[:300]
    }
except Exception as e:
    results["options_contracts"] = {"error": str(e)}

try:
    r = requests.get(OPTIONS_SNAP_URL.format("SPY"), headers=HEADERS,
                     params={"type": "call", "limit": 3}, timeout=10)
    results["options_snapshots"] = {
        "status": r.status_code,
        "body":   r.json() if r.status_code == 200 else r.text[:500]
    }
except Exception as e:
    results["options_snapshots"] = {"error": str(e)}

return jsonify(results)
```

@app.route(”/telegram-test”)
def telegram_test():
ok = send_telegram_alert(“Test from your 0DTE Engine - Telegram is working!”)
return jsonify({
“sent”:         ok,
“token_length”: len(os.getenv(“TELEGRAM_BOT_TOKEN”, “”)),
“chat_id”:      os.getenv(“TELEGRAM_CHAT_ID”, “”),
“log”:          list(debug_log)
})

# =============================================

# STARTUP

# =============================================

scheduler_thread = threading.Thread(target=background_scheduler, daemon=True)
scheduler_thread.start()

if **name** == “**main**”:
port = int(os.environ.get(“PORT”, 8000))
app.run(host=“0.0.0.0”, port=port)
