from flask import Flask, jsonify, render_template_string, request, redirect
import requests
import os
import statistics
import threading
import time
import json
import sqlite3
from datetime import datetime
import pytz

# =============================================
# APP SETUP
# =============================================

app = Flask(__name__)

ACCOUNT_SIZE  = 30000
SCAN_INTERVAL = 300
BOT_ENABLED   = True

SYMBOLS = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "AMD", "META", "MSFT", "AMZN"]

ALPACA_KEY    = os.getenv("APCA_API_KEY_ID", "").strip()
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY", "").strip()

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET
}

DATA_URL         = "https://data.alpaca.markets/v2/stocks/{}/bars"
QUOTE_URL        = "https://data.alpaca.markets/v2/stocks/{}/quotes/latest"
CLOCK_URL        = "https://paper-api.alpaca.markets/v2/clock"
OPTIONS_URL      = "https://data.alpaca.markets/v1beta1/options/contracts"
OPTIONS_SNAP_URL = "https://data.alpaca.markets/v1beta1/options/snapshots/{}"

ALERT_FILE = "/tmp/last_alert.json"
CACHE_FILE = "/tmp/scan_cache.json"
DB_FILE    = "/tmp/trades.db"

state_lock   = threading.Lock()
debug_log    = []
all_signals  = []   # ranked list of all symbol results
next_scan_at = 0
bot_enabled  = True


# =============================================
# LOGGING
# =============================================

def log(msg):
    ts    = datetime.now(pytz.utc).strftime("%H:%M:%S")
    entry = "[{}] {}".format(ts, msg)
    print(entry)
    with state_lock:
        debug_log.append(entry)
        if len(debug_log) > 150:
            debug_log.pop(0)


# =============================================
# DATABASE
# =============================================

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c    = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ts        TEXT,
            symbol    TEXT,
            direction TEXT,
            price     REAL,
            score     REAL,
            premium   REAL,
            strike    TEXT,
            contracts INTEGER,
            stop      REAL,
            target    REAL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT,
            symbol      TEXT,
            direction   TEXT,
            premium     REAL,
            contracts   INTEGER,
            stop        REAL,
            target      REAL,
            outcome     TEXT,
            exit_price  REAL,
            pnl         REAL,
            r_mult      REAL
        )
    """)
    conn.commit()
    conn.close()


def db_log_signal(sig):
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        c.execute("""
            INSERT INTO signals (ts,symbol,direction,price,score,premium,strike,contracts,stop,target)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now(pytz.utc).isoformat(),
            sig.get("symbol"), sig.get("direction"),
            sig.get("price"),  sig.get("score"),
            sig.get("premium"), str(sig.get("strike","")),
            sig.get("contracts"), sig.get("stop"), sig.get("target")
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        log("DB signal log error: {}".format(e))


def db_log_trade(symbol, direction, premium, contracts, stop, target):
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        c.execute("""
            INSERT INTO trades (ts,symbol,direction,premium,contracts,stop,target,outcome)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            datetime.now(pytz.utc).isoformat(),
            symbol, direction, premium, contracts, stop, target, "OPEN"
        ))
        trade_id = c.lastrowid
        conn.commit()
        conn.close()
        return trade_id
    except Exception as e:
        log("DB trade log error: {}".format(e))
        return None


def db_close_trade(trade_id, exit_price, outcome):
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        c.execute("SELECT premium, contracts FROM trades WHERE id=?", (trade_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return
        premium, contracts = row
        if outcome == "WIN":
            pnl    = (exit_price - premium) * 100 * contracts
            r_mult = (exit_price - premium) / (premium * 0.45)
        else:
            pnl    = (exit_price - premium) * 100 * contracts
            r_mult = (exit_price - premium) / (premium * 0.45)
        c.execute("""
            UPDATE trades SET outcome=?, exit_price=?, pnl=?, r_mult=?
            WHERE id=?
        """, (outcome, exit_price, round(pnl, 2), round(r_mult, 2), trade_id))
        conn.commit()
        conn.close()
        log("Trade {} closed: {} pnl={}".format(trade_id, outcome, round(pnl,2)))
    except Exception as e:
        log("DB close trade error: {}".format(e))


def db_get_today_trades():
    try:
        et    = pytz.timezone("America/New_York")
        today = datetime.now(et).strftime("%Y-%m-%d")
        conn  = sqlite3.connect(DB_FILE)
        c     = conn.cursor()
        c.execute("""
            SELECT id,symbol,direction,premium,contracts,stop,target,outcome,exit_price,pnl,r_mult,ts
            FROM trades WHERE ts LIKE ?
            ORDER BY ts DESC
        """, (today + "%",))
        rows = c.fetchall()
        conn.close()
        cols = ["id","symbol","direction","premium","contracts",
                "stop","target","outcome","exit_price","pnl","r_mult","ts"]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        log("DB get trades error: {}".format(e))
        return []


def db_get_open_trades():
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        c.execute("""
            SELECT id,symbol,direction,premium,contracts,stop,target,ts
            FROM trades WHERE outcome='OPEN'
            ORDER BY ts DESC
        """)
        rows = c.fetchall()
        conn.close()
        cols = ["id","symbol","direction","premium","contracts","stop","target","ts"]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        log("DB open trades error: {}".format(e))
        return []


# =============================================
# ALERT PERSISTENCE
# =============================================

def load_last_alert():
    try:
        with open(ALERT_FILE, "r") as f:
            data = json.load(f)
            return data.get("alert_id", ""), data.get("date", "")
    except:
        return "", ""


def save_last_alert(alert_id, date_str):
    try:
        with open(ALERT_FILE, "w") as f:
            json.dump({"alert_id": alert_id, "date": date_str}, f)
    except Exception as e:
        log("Could not save alert state: {}".format(e))


def should_alert(symbol, direction):
    et       = pytz.timezone("America/New_York")
    today    = datetime.now(et).strftime("%Y-%m-%d")
    alert_id = "{}_{}".format(symbol, direction)
    saved_id, saved_date = load_last_alert()
    if saved_id == alert_id and saved_date == today:
        log("Alert suppressed: same signal already sent today")
        return False
    save_last_alert(alert_id, today)
    return True


# =============================================
# TELEGRAM
# =============================================

def send_telegram(message):
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log("Telegram not configured")
        return False
    url = "https://api.telegram.org/bot{}/sendMessage".format(token)
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=10)
        log("Telegram HTTP {}: {}".format(resp.status_code, resp.text[:100]))
        return resp.status_code == 200
    except Exception as e:
        log("Telegram exception: {}".format(e))
        return False


def get_telegram_updates(offset=0):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return [], offset
    try:
        url  = "https://api.telegram.org/bot{}/getUpdates".format(token)
        resp = requests.get(url, params={"offset": offset, "timeout": 10}, timeout=15)
        if resp.status_code != 200:
            return [], offset
        updates = resp.json().get("result", [])
        new_offset = offset
        if updates:
            new_offset = updates[-1]["update_id"] + 1
        return updates, new_offset
    except:
        return [], offset


def handle_telegram_command(text):
    global bot_enabled
    text = text.strip().lower()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if text in ("/stop", "stop"):
        bot_enabled = False
        send_telegram("Bot PAUSED. Send /start to resume scanning.")

    elif text in ("/start", "start"):
        bot_enabled = True
        send_telegram("Bot RESUMED. Scanning every 5 minutes.")

    elif text in ("/status", "status"):
        with state_lock:
            sigs = list(all_signals)
        if sigs:
            best = sigs[0]
            if best.get("direction"):
                msg = "SIGNAL: {} {} | Score: {} | Premium: ${}".format(
                    best["symbol"], best["direction"],
                    best.get("score","?"), best.get("premium","?"))
            else:
                msg = "No active breakout signals right now."
        else:
            msg = "No scan data yet."
        send_telegram(msg)

    elif text in ("/pnl", "pnl"):
        trades = db_get_today_trades()
        closed = [t for t in trades if t["outcome"] != "OPEN"]
        total_pnl = sum(t["pnl"] or 0 for t in closed)
        wins  = len([t for t in closed if t["outcome"] == "WIN"])
        losses = len([t for t in closed if t["outcome"] == "LOSS"])
        msg = "TODAY P&L\nTrades: {} | W: {} L: {}\nTotal: ${}".format(
            len(closed), wins, losses, round(total_pnl, 2))
        send_telegram(msg)

    elif text in ("/help", "help"):
        send_telegram(
            "Commands:\n"
            "/status - current signal\n"
            "/pnl - today P&L\n"
            "/stop - pause bot\n"
            "/start - resume bot\n"
            "/help - this message"
        )


# =============================================
# MARKET OPEN
# =============================================

def market_open():
    try:
        r = requests.get(CLOCK_URL, headers=HEADERS, timeout=5)
        log("Clock HTTP {}".format(r.status_code))
        if r.status_code == 200:
            clock = r.json()
            log("Clock: {}".format(clock))
            return clock.get("is_open", False)
        log("Clock error: {}".format(r.text[:100]))
    except Exception as e:
        log("Clock exception: {}".format(e))
    et    = pytz.timezone("America/New_York")
    now   = datetime.now(et)
    if now.weekday() >= 5:
        return False
    start = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    end   = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    return start <= now <= end


# =============================================
# DATA FETCHING
# =============================================

def get_intraday(symbol):
    try:
        r = requests.get(DATA_URL.format(symbol), headers=HEADERS,
                         params={"timeframe": "5Min", "limit": 50}, timeout=10)
        if r.status_code != 200:
            return None
        return r.json().get("bars", [])
    except:
        return None


def get_daily(symbol):
    try:
        r = requests.get(DATA_URL.format(symbol), headers=HEADERS,
                         params={"timeframe": "1Day", "limit": 20}, timeout=10)
        if r.status_code != 200:
            return None
        return r.json().get("bars", [])
    except:
        return None


def get_current_price(symbol):
    try:
        r = requests.get(QUOTE_URL.format(symbol), headers=HEADERS, timeout=5)
        if r.status_code == 200:
            q = r.json().get("quote", {})
            ap = q.get("ap", 0)
            bp = q.get("bp", 0)
            if ap and bp:
                return round((ap + bp) / 2, 2)
    except:
        pass
    return None


# =============================================
# INDICATORS
# =============================================

def calculate_vwap(bars):
    pv = vol = 0
    for b in bars:
        typ  = (b["h"] + b["l"] + b["c"]) / 3
        pv  += typ * b["v"]
        vol += b["v"]
    return pv / vol if vol else None


def volatility_regime(daily_bars):
    if len(daily_bars) < 5:
        return False
    ranges    = [b["h"] - b["l"] for b in daily_bars]
    today_rng = ranges[-1]
    avg_rng   = statistics.mean(ranges[:-1])
    return today_rng > avg_rng * 0.75


# =============================================
# OPTIONS
# =============================================

def get_liquid_option(symbol, direction):
    option_type = "call" if direction == "CALL" else "put"
    try:
        r = requests.get(OPTIONS_URL, headers=HEADERS,
                         params={"underlying_symbols": symbol, "type": option_type,
                                 "status": "active", "limit": 100}, timeout=10)
        log("Options/contracts {} {}: HTTP {}".format(symbol, option_type, r.status_code))
        if r.status_code == 200:
            contracts = r.json().get("option_contracts", [])
            filtered  = [c for c in contracts if float(c.get("close_price") or 0) > 0.10]
            filtered.sort(key=lambda x: x.get("open_interest", 0), reverse=True)
            if filtered:
                best = filtered[0]
                return float(best["close_price"]), best["strike_price"]
        else:
            log("Options error: {}".format(r.text[:150]))
    except Exception as e:
        log("Options exception: {}".format(e))

    try:
        r2 = requests.get(OPTIONS_SNAP_URL.format(symbol), headers=HEADERS,
                          params={"type": option_type, "limit": 100}, timeout=10)
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
            if candidates:
                best = candidates[0]
                try:
                    strike = int(best["symbol"][-8:]) / 1000
                except:
                    strike = "N/A"
                return best["price"], strike
    except Exception as e:
        log("Options snapshots exception: {}".format(e))

    return None, None


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
# SCANNER - returns ALL symbol results ranked
# =============================================

def scan_all_symbols():
    results = []

    for symbol in SYMBOLS:
        result = {
            "symbol":    symbol,
            "direction": None,
            "score":     0,
            "price":     None,
            "premium":   None,
            "strike":    None,
            "contracts": None,
            "stop":      None,
            "target":    None,
            "status":    "scanning",
            "vwap":      None,
            "orb_high":  None,
            "orb_low":   None,
        }

        intraday = get_intraday(symbol)
        daily    = get_daily(symbol)

        if not intraday or len(intraday) < 5 or not daily:
            result["status"] = "no data"
            results.append(result)
            continue

        if not volatility_regime(daily):
            result["status"] = "low volatility"
            results.append(result)
            continue

        orb_high = max(b["h"] for b in intraday[:3])
        orb_low  = min(b["l"] for b in intraday[:3])
        current  = intraday[-1]
        price    = current["c"]
        vwap     = calculate_vwap(intraday)

        result["price"]    = round(price, 2)
        result["vwap"]     = round(vwap, 2) if vwap else None
        result["orb_high"] = round(orb_high, 2)
        result["orb_low"]  = round(orb_low, 2)

        if not vwap:
            result["status"] = "no vwap"
            results.append(result)
            continue

        direction         = None
        breakout_strength = 0

        if price > orb_high and price > vwap:
            direction         = "CALL"
            breakout_strength = (price - orb_high) / orb_high
        elif price < orb_low and price < vwap:
            direction         = "PUT"
            breakout_strength = (orb_low - price) / orb_low

        if not direction:
            result["status"] = "no breakout"
            results.append(result)
            continue

        vol_ratio = current["v"] / intraday[-2]["v"] if intraday[-2]["v"] > 0 else 1
        score     = breakout_strength * 100 + vol_ratio

        premium, strike = get_liquid_option(symbol, direction)
        if premium:
            contracts, stop, target = calculate_contracts(premium, score)
            result["premium"]   = round(premium, 2)
            result["strike"]    = strike
            result["contracts"] = contracts
            result["stop"]      = stop
            result["target"]    = target
        else:
            result["status"] = "no option data"

        result["direction"] = direction
        result["score"]     = round(score, 2)
        result["status"]    = "signal" if premium else "signal (no options)"
        results.append(result)

    results.sort(key=lambda x: x["score"], reverse=True)
    return results


# =============================================
# MAIN SCAN RUNNER
# =============================================

def run_signal_scan():
    global all_signals, next_scan_at
    log("=== Running signal scan ===")
    log("Key set: {} | Secret set: {} | Bot enabled: {}".format(
        bool(ALPACA_KEY), bool(ALPACA_SECRET), bot_enabled))

    if not market_open():
        log("Market closed - skipping scan")
        with state_lock:
            next_scan_at = time.time() + SCAN_INTERVAL
        return

    results = scan_all_symbols()

    with state_lock:
        all_signals  = results
        next_scan_at = time.time() + SCAN_INTERVAL

    # Alert on best signal only
    best = next((r for r in results if r["status"] == "signal"), None)
    if best and bot_enabled:
        if should_alert(best["symbol"], best["direction"]):
            db_log_signal(best)
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
                "Target: ${}\n\n"
                "Dashboard: {}tion.up.railway.app"
            ).format(
                best["symbol"], best["direction"], best["score"],
                best["price"], best["strike"], best["premium"],
                best["contracts"], best["stop"], best["target"],
                ""
            )
            send_telegram(msg)

    log("Scan complete. {} symbols scanned, {} signals found.".format(
        len(results),
        len([r for r in results if "signal" in r.get("status","")])))


# =============================================
# BACKGROUND THREADS
# =============================================

def background_scheduler():
    log("Background scheduler started")
    time.sleep(10)
    while True:
        try:
            run_signal_scan()
        except Exception as e:
            log("Scheduler error: {}".format(e))
        time.sleep(SCAN_INTERVAL)


def telegram_poller():
    log("Telegram poller started")
    offset = 0
    time.sleep(15)
    while True:
        try:
            updates, offset = get_telegram_updates(offset)
            for update in updates:
                msg  = update.get("message", {})
                text = msg.get("text", "")
                if text:
                    log("Telegram command received: {}".format(text))
                    handle_telegram_command(text)
        except Exception as e:
            log("Telegram poller error: {}".format(e))
        time.sleep(3)


# =============================================
# DASHBOARD HTML
# =============================================

def render_dashboard():
    with state_lock:
        signals = list(all_signals)
        secs    = max(0, int(next_scan_at - time.time()))
        logs    = list(debug_log[-30:])

    trades      = db_get_today_trades()
    open_trades = db_get_open_trades()
    closed      = [t for t in trades if t["outcome"] != "OPEN"]
    total_pnl   = sum(t["pnl"] or 0 for t in closed)
    wins        = len([t for t in closed if t["outcome"] == "WIN"])
    losses      = len([t for t in closed if t["outcome"] == "LOSS"])

    is_open = market_open()
    market_color  = "green" if is_open else "red"
    market_status = "OPEN" if is_open else "CLOSED"

    # Build signal rows
    signal_rows = ""
    for s in signals:
        status = s.get("status", "")
        sym    = s["symbol"]
        price  = s.get("price", "-")
        score  = s.get("score", 0)

        if status == "signal":
            d     = s["direction"]
            color = "green" if d == "CALL" else "red"
            row   = (
                "<tr style='border-bottom:1px solid #21262d'>"
                "<td style='padding:10px'><b>{}</b></td>"
                "<td style='color:{};padding:10px'><b>{}</b></td>"
                "<td style='padding:10px'>${}</td>"
                "<td style='padding:10px'>{}</td>"
                "<td style='padding:10px'>${}</td>"
                "<td style='padding:10px'>${} / ${}</td>"
                "<td style='padding:10px'>"
                "<a href='/take?sym={}&dir={}&prem={}&con={}&stp={}&tgt={}' "
                "style='background:#238636;color:white;padding:5px 10px;"
                "border-radius:5px;text-decoration:none;font-size:12px'>TAKE</a>"
                "</td>"
                "</tr>"
            ).format(
                sym, color, d, price, score,
                s.get("premium","-"),
                s.get("stop","-"), s.get("target","-"),
                sym, d, s.get("premium",""), s.get("contracts",""),
                s.get("stop",""), s.get("target","")
            )
        elif status == "signal (no options)":
            row = (
                "<tr style='border-bottom:1px solid #21262d;opacity:0.7'>"
                "<td style='padding:10px'><b>{}</b></td>"
                "<td style='color:{};padding:10px'>{}</td>"
                "<td style='padding:10px'>${}</td>"
                "<td style='padding:10px'>{}</td>"
                "<td colspan='3' style='padding:10px;color:#e3b341'>No option data</td>"
                "</tr>"
            ).format(sym,
                     "green" if s.get("direction")=="CALL" else "red",
                     s.get("direction",""), price, score)
        else:
            row = (
                "<tr style='border-bottom:1px solid #21262d;opacity:0.4'>"
                "<td style='padding:10px'>{}</td>"
                "<td colspan='6' style='padding:10px;color:#8b949e'>{}</td>"
                "</tr>"
            ).format(sym, status)

        signal_rows += row

    # Build open trades rows
    open_rows = ""
    for t in open_trades:
        current_price = get_current_price(t["symbol"])
        if current_price and t["premium"]:
            unreal = round((current_price - t["premium"]) * 100 * t["contracts"], 2)
            unreal_color = "green" if unreal >= 0 else "red"
            unreal_str = "<span style='color:{}'>${}</span>".format(unreal_color, unreal)
        else:
            unreal_str = "<span style='color:#8b949e'>-</span>"

        open_rows += (
            "<tr style='border-bottom:1px solid #21262d'>"
            "<td style='padding:8px'>{}</td>"
            "<td style='padding:8px'>{}</td>"
            "<td style='padding:8px'>${}</td>"
            "<td style='padding:8px'>{}x</td>"
            "<td style='padding:8px'>{}</td>"
            "<td style='padding:8px'>"
            "<a href='/close?id={}&outcome=WIN&exit={}' "
            "style='background:#238636;color:white;padding:4px 8px;"
            "border-radius:4px;text-decoration:none;font-size:11px;margin-right:4px'>WIN</a>"
            "<a href='/close?id={}&outcome=LOSS&exit={}' "
            "style='background:#da3633;color:white;padding:4px 8px;"
            "border-radius:4px;text-decoration:none;font-size:11px'>LOSS</a>"
            "</td>"
            "</tr>"
        ).format(
            t["symbol"], t["direction"], t["premium"], t["contracts"],
            unreal_str,
            t["id"], current_price or 0,
            t["id"], current_price or 0
        )

    # Build closed trades rows
    closed_rows = ""
    for t in closed:
        pnl_color = "green" if (t["pnl"] or 0) >= 0 else "red"
        closed_rows += (
            "<tr style='border-bottom:1px solid #21262d'>"
            "<td style='padding:8px'>{}</td>"
            "<td style='padding:8px'>{}</td>"
            "<td style='padding:8px'>${}</td>"
            "<td style='padding:8px'>{}</td>"
            "<td style='padding:8px;color:{}'>${}</td>"
            "</tr>"
        ).format(
            t["symbol"], t["direction"], t["premium"],
            t["outcome"], pnl_color, t["pnl"] or 0
        )

    pnl_color = "green" if total_pnl >= 0 else "red"

    html = """
<!DOCTYPE html>
<html>
<head>
<meta http-equiv='refresh' content='30'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<style>
  body{{background:#0d1117;color:white;font-family:Arial,sans-serif;padding:15px;margin:0}}
  h1{{font-size:18px;margin-bottom:5px}}
  h2{{font-size:14px;color:#8b949e;margin:20px 0 8px 0}}
  .card{{background:#161b22;border-radius:10px;margin-bottom:15px;overflow:hidden}}
  .card-header{{padding:12px 15px;border-bottom:1px solid #21262d;
               display:flex;justify-content:space-between;align-items:center}}
  .green{{color:#3fb950}} .red{{color:#f85149}} .yellow{{color:#e3b341}}
  .badge{{font-size:11px;padding:3px 8px;border-radius:10px;font-weight:bold}}
  .badge-green{{background:#238636}} .badge-red{{background:#da3633}}
  table{{width:100%;border-collapse:collapse;font-size:13px}}
  th{{padding:10px;text-align:left;color:#8b949e;border-bottom:1px solid #21262d}}
  .debug{{background:#0a0d12;padding:12px;border-radius:8px;font-size:10px;
         font-family:monospace;max-height:200px;overflow-y:auto;color:#8b949e}}
  .stat-row{{display:flex;gap:10px;margin-bottom:15px}}
  .stat{{background:#161b22;border-radius:8px;padding:15px;flex:1;text-align:center}}
  .stat-val{{font-size:22px;font-weight:bold}}
  .stat-lbl{{font-size:11px;color:#8b949e;margin-top:4px}}
  a.nav{{color:#58a6ff;text-decoration:none;font-size:12px;margin-right:10px}}
</style>
</head>
<body>
<h1>Institutional 0DTE Engine</h1>
<div style='margin-bottom:12px;font-size:12px;color:#8b949e'>
  Market: <span class='{mcolor}'>{mstatus}</span> &nbsp;|&nbsp;
  Next scan: {secs}s &nbsp;|&nbsp;
  Bot: <span class='{bcolor}'>{benabled}</span> &nbsp;|&nbsp;
  <a class='nav' href='/alpaca-test'>Alpaca</a>
  <a class='nav' href='/telegram-test'>Telegram</a>
  <a class='nav' href='/debug'>Debug</a>
</div>

<div class='stat-row'>
  <div class='stat'>
    <div class='stat-val {pcolor}'>${pnl}</div>
    <div class='stat-lbl'>Today P&L</div>
  </div>
  <div class='stat'>
    <div class='stat-val'>{ntrades}</div>
    <div class='stat-lbl'>Trades</div>
  </div>
  <div class='stat'>
    <div class='stat-val green'>{nwins}</div>
    <div class='stat-lbl'>Wins</div>
  </div>
  <div class='stat'>
    <div class='stat-val red'>{nlosses}</div>
    <div class='stat-lbl'>Losses</div>
  </div>
</div>

<div class='card'>
  <div class='card-header'>
    <span>Signal Scanner</span>
    <span style='font-size:12px;color:#8b949e'>{nsymbols} symbols</span>
  </div>
  <table>
    <tr>
      <th>Symbol</th><th>Dir</th><th>Price</th>
      <th>Score</th><th>Premium</th><th>Stop/Target</th><th>Action</th>
    </tr>
    {signal_rows}
  </table>
</div>

<div class='card'>
  <div class='card-header'><span>Open Trades</span></div>
  <table>
    <tr><th>Symbol</th><th>Dir</th><th>Entry</th><th>Size</th><th>Unreal P&L</th><th>Close</th></tr>
    {open_rows}
  </table>
</div>

<div class='card'>
  <div class='card-header'><span>Today Closed</span></div>
  <table>
    <tr><th>Symbol</th><th>Dir</th><th>Entry</th><th>Result</th><th>P&L</th></tr>
    {closed_rows}
  </table>
</div>

<div class='card' style='padding:12px'>
  <div style='color:#8b949e;font-size:12px;margin-bottom:6px'>Debug Log</div>
  <div class='debug'>{log_lines}</div>
</div>

</body></html>
""".format(
        mcolor=market_color, mstatus=market_status,
        secs=secs,
        bcolor="green" if bot_enabled else "red",
        benabled="ON" if bot_enabled else "PAUSED",
        pcolor=pnl_color,
        pnl=round(total_pnl, 2),
        ntrades=len(closed),
        nwins=wins,
        nlosses=losses,
        nsymbols=len(signals),
        signal_rows=signal_rows if signal_rows else "<tr><td colspan='7' style='padding:15px;color:#8b949e;text-align:center'>Waiting for market open or scan...</td></tr>",
        open_rows=open_rows if open_rows else "<tr><td colspan='6' style='padding:15px;color:#8b949e;text-align:center'>No open trades</td></tr>",
        closed_rows=closed_rows if closed_rows else "<tr><td colspan='5' style='padding:15px;color:#8b949e;text-align:center'>No closed trades today</td></tr>",
        log_lines="<br>".join(logs) if logs else "No logs yet"
    )

    return html


# =============================================
# ROUTES
# =============================================

@app.route("/")
def home():
    return render_dashboard()


@app.route("/take")
def take_trade():
    """User clicks TAKE on a signal - logs it as an open trade."""
    sym  = request.args.get("sym", "")
    dir_ = request.args.get("dir", "")
    prem = request.args.get("prem", "0")
    con  = request.args.get("con", "1")
    stp  = request.args.get("stp", "0")
    tgt  = request.args.get("tgt", "0")
    try:
        db_log_trade(sym, dir_, float(prem), int(con), float(stp), float(tgt))
        log("Trade taken: {} {} prem={}".format(sym, dir_, prem))
        send_telegram("TRADE TAKEN: {} {} | Entry: ${} | {}x contracts | Stop: ${} | Target: ${}".format(
            sym, dir_, prem, con, stp, tgt))
    except Exception as e:
        log("Take trade error: {}".format(e))
    return redirect("/")


@app.route("/close")
def close_trade():
    """User clicks WIN or LOSS on open trade."""
    trade_id = request.args.get("id", "")
    outcome  = request.args.get("outcome", "")
    exit_p   = request.args.get("exit", "0")
    try:
        db_close_trade(int(trade_id), float(exit_p), outcome)
        send_telegram("TRADE CLOSED: {} | Exit: ${} | Result: {}".format(
            trade_id, exit_p, outcome))
    except Exception as e:
        log("Close trade error: {}".format(e))
    return redirect("/")


@app.route("/debug")
def debug_route():
    with state_lock:
        return jsonify({"signals": all_signals, "log": debug_log[-50:]})


@app.route("/alpaca-test")
def alpaca_test():
    results = {}
    try:
        r = requests.get(CLOCK_URL, headers=HEADERS, timeout=5)
        results["clock"] = {"status": r.status_code,
                             "body": r.json() if r.status_code==200 else r.text}
    except Exception as e:
        results["clock"] = {"error": str(e)}
    try:
        r = requests.get(DATA_URL.format("SPY"), headers=HEADERS,
                         params={"timeframe":"5Min","limit":3}, timeout=10)
        results["spy_bars"] = {"status": r.status_code,
                                "body": r.json() if r.status_code==200 else r.text[:300]}
    except Exception as e:
        results["spy_bars"] = {"error": str(e)}
    try:
        r = requests.get(OPTIONS_URL, headers=HEADERS,
                         params={"underlying_symbols":"SPY","type":"call",
                                 "status":"active","limit":3}, timeout=10)
        results["options_contracts"] = {"status": r.status_code,
                                         "body": r.json() if r.status_code==200 else r.text[:300]}
    except Exception as e:
        results["options_contracts"] = {"error": str(e)}
    try:
        r = requests.get(OPTIONS_SNAP_URL.format("SPY"), headers=HEADERS,
                         params={"type":"call","limit":3}, timeout=10)
        results["options_snapshots"] = {"status": r.status_code,
                                         "body": r.json() if r.status_code==200 else r.text[:500]}
    except Exception as e:
        results["options_snapshots"] = {"error": str(e)}
    return jsonify(results)


@app.route("/telegram-test")
def telegram_test():
    ok = send_telegram("Test from your 0DTE Engine - Telegram is working!")
    return jsonify({
        "sent":         ok,
        "token_length": len(os.getenv("TELEGRAM_BOT_TOKEN","")),
        "chat_id":      os.getenv("TELEGRAM_CHAT_ID",""),
        "log":          debug_log[-20:]
    })


# =============================================
# STARTUP
# =============================================

init_db()

threading.Thread(target=background_scheduler, daemon=True).start()
threading.Thread(target=telegram_poller,      daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
