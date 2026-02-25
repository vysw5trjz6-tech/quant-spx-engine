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
ORB_BARS      = 6       # 30 min ORB (6 x 5min bars) - institutional standard

SYMBOLS = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "AMD", "META", "MSFT", "AMZN"]

ALPACA_KEY    = os.getenv("APCA_API_KEY_ID", "").strip()
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY", "").strip()

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET
}

DATA_URL  = "https://data.alpaca.markets/v2/stocks/{}/bars"
QUOTE_URL = "https://data.alpaca.markets/v2/stocks/{}/quotes/latest"
CLOCK_URL = "https://paper-api.alpaca.markets/v2/clock"

# Tradier - options data source (real ATM 0DTE chains)
TRADIER_TOKEN   = os.getenv("TRADIER_TOKEN", "").strip()
TRADIER_URL     = "https://sandbox.tradier.com/v1"
TRADIER_HEADERS = {
    "Authorization": "Bearer {}".format(os.getenv("TRADIER_TOKEN", "").strip()),
    "Accept":        "application/json"
}

ALERT_FILE = "/tmp/last_alert.json"
DB_FILE    = "/tmp/trades.db"

state_lock   = threading.Lock()
debug_log    = []
all_signals  = []
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
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT,
            symbol     TEXT,
            direction  TEXT,
            premium    REAL,
            contracts  INTEGER,
            stop       REAL,
            target     REAL,
            outcome    TEXT,
            exit_price REAL,
            pnl        REAL,
            r_mult     REAL,
            grade      TEXT,
            grade_pts  INTEGER,
            gap_pct    REAL,
            gap_dir    TEXT,
            rs         REAL,
            entry_hour REAL
        )
    """)
    # Migrate existing tables that may not have new columns
    for col, coltype in [("grade","TEXT"), ("grade_pts","INTEGER"),
                          ("gap_pct","REAL"), ("gap_dir","TEXT"),
                          ("rs","REAL"), ("entry_hour","REAL")]:
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN {} {}".format(col, coltype))
        except:
            pass
    conn.commit()
    conn.close()


def db_log_signal(sig):
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        c.execute("""
            INSERT INTO signals
            (ts,symbol,direction,price,score,premium,strike,contracts,stop,target)
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


def db_log_trade(symbol, direction, premium, contracts, stop, target,
                  grade=None, grade_pts=None, gap_pct=None,
                  gap_dir=None, rs=None, entry_hour=None):
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        et   = pytz.timezone("America/New_York")
        if entry_hour is None:
            now        = datetime.now(et)
            entry_hour = round(now.hour + now.minute / 60.0, 2)
        c.execute("""
            INSERT INTO trades
            (ts,symbol,direction,premium,contracts,stop,target,outcome,
             grade,grade_pts,gap_pct,gap_dir,rs,entry_hour)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now(pytz.utc).isoformat(),
            symbol, direction, premium, contracts, stop, target, "OPEN",
            grade, grade_pts, gap_pct, gap_dir, rs, entry_hour
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
            SELECT id,symbol,direction,premium,contracts,stop,target,
                   outcome,exit_price,pnl,r_mult,ts
            FROM trades WHERE ts LIKE ?
            ORDER BY ts DESC
        """, (today + "%",))
        rows = c.fetchall()
        conn.close()
        cols = ["id","symbol","direction","premium","contracts","stop",
                "target","outcome","exit_price","pnl","r_mult","ts"]
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
    # Validate token format: must contain exactly one colon
    if token.count(":") != 1:
        log("Telegram token malformed - must contain exactly one colon")
        return False
    bot_id, bot_hash = token.split(":", 1)
    if not bot_id.isdigit():
        log("Telegram token malformed - part before colon must be numeric")
        return False
    url = "https://api.telegram.org/bot{}/sendMessage".format(token)
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": message},
                             timeout=10)
        log("Telegram HTTP {}: {}".format(resp.status_code, resp.text[:150]))
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
        updates    = resp.json().get("result", [])
        new_offset = offset
        if updates:
            new_offset = updates[-1]["update_id"] + 1
        return updates, new_offset
    except:
        return [], offset


def handle_telegram_command(text):
    global bot_enabled
    text = text.strip().lower()

    if text in ("/stop", "stop"):
        bot_enabled = False
        send_telegram("Bot PAUSED. Send /start to resume scanning.")

    elif text in ("/start", "start"):
        bot_enabled = True
        send_telegram("Bot RESUMED. Scanning every 5 minutes.")

    elif text in ("/status", "status"):
        with state_lock:
            sigs = list(all_signals)
        active = [s for s in sigs if s.get("status") in ("SIGNAL","WATCHING")]
        if active:
            lines = []
            for s in active[:3]:
                lines.append("{} {} | {} | Score: {}".format(
                    s["symbol"], s.get("direction","?"),
                    s["status"], s.get("score","?")))
            send_telegram("TOP SETUPS:\n" + "\n".join(lines))
        else:
            send_telegram("No setups right now. Market may be in consolidation.")

    elif text in ("/pnl", "pnl"):
        trades    = db_get_today_trades()
        closed    = [t for t in trades if t["outcome"] != "OPEN"]
        total_pnl = sum(t["pnl"] or 0 for t in closed)
        wins      = len([t for t in closed if t["outcome"] == "WIN"])
        losses    = len([t for t in closed if t["outcome"] == "LOSS"])
        send_telegram("TODAY P&L\nTrades: {} | W: {} L: {}\nTotal: ${}".format(
            len(closed), wins, losses, round(total_pnl, 2)))

    elif text in ("/help", "help"):
        send_telegram(
            "Commands:\n"
            "/status - top current setups\n"
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
                         params={"timeframe": "5Min", "limit": 78}, timeout=10)
        if r.status_code != 200:
            log("Intraday {} error: {}".format(symbol, r.text[:80]))
            return None
        bars = r.json().get("bars", [])
        log("Intraday {}: {} bars".format(symbol, len(bars)))
        return bars
    except Exception as e:
        log("Intraday exception {}: {}".format(symbol, e))
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
            q  = r.json().get("quote", {})
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


def volatility_score(daily_bars):
    """
    Returns a multiplier (0.5 to 1.5) based on today's range vs average.
    No longer a hard block - just modifies signal score.
    Only returns 0 on truly dead days (< 30% of average range).
    """
    if len(daily_bars) < 5:
        return 1.0
    ranges    = [b["h"] - b["l"] for b in daily_bars]
    today_rng = ranges[-1]
    avg_rng   = statistics.mean(ranges[:-1])
    if avg_rng == 0:
        return 1.0
    ratio = today_rng / avg_rng
    log("  Vol ratio: {:.2f} (today={:.2f} avg={:.2f})".format(
        ratio, today_rng, avg_rng))
    if ratio < 0.30:
        return 0.0    # truly dead day - skip
    elif ratio < 0.60:
        return 0.6    # below avg - reduce score
    elif ratio < 0.85:
        return 0.85   # slightly below avg - small reduction
    elif ratio <= 1.20:
        return 1.0    # normal
    else:
        return 1.3    # high vol day - bonus


# =============================================
# GAP ANALYSIS
# =============================================

def get_premarket_gap(daily_bars, intraday_bars):
    """
    Gap % = (today open - yesterday close) / yesterday close * 100
    Uses first intraday bar open vs last daily bar close.
    Returns (gap_pct, gap_direction) e.g. (1.23, "UP") or (-0.85, "DOWN")
    """
    if not daily_bars or not intraday_bars:
        return 0.0, "FLAT"
    prev_close  = daily_bars[-1]["c"]
    today_open  = intraday_bars[0]["o"]
    if prev_close == 0:
        return 0.0, "FLAT"
    gap_pct = round((today_open - prev_close) / prev_close * 100, 3)
    if gap_pct > 0.3:
        direction = "UP"
    elif gap_pct < -0.3:
        direction = "DOWN"
    else:
        direction = "FLAT"
    return gap_pct, direction


# =============================================
# SPY RELATIVE STRENGTH
# =============================================

_spy_cache = {"bars": None, "ts": 0}

def get_spy_change():
    """
    Returns SPY intraday % change from open.
    Cached for 60s to avoid repeated API calls during full scan.
    """
    global _spy_cache
    now = time.time()
    if _spy_cache["bars"] and now - _spy_cache["ts"] < 60:
        bars = _spy_cache["bars"]
    else:
        bars = get_intraday("SPY")
        _spy_cache = {"bars": bars, "ts": now}
    if not bars or len(bars) < 2:
        return 0.0
    open_price = bars[0]["o"]
    last_price = bars[-1]["c"]
    if open_price == 0:
        return 0.0
    return round((last_price - open_price) / open_price * 100, 3)


def get_symbol_change(intraday_bars):
    """Intraday % change from open for a symbol."""
    if not intraday_bars or len(intraday_bars) < 2:
        return 0.0
    open_price = intraday_bars[0]["o"]
    last_price = intraday_bars[-1]["c"]
    if open_price == 0:
        return 0.0
    return round((last_price - open_price) / open_price * 100, 3)


def relative_strength(symbol_change, spy_change):
    """
    RS = symbol % change - SPY % change.
    Positive = outperforming SPY (good for CALL).
    Negative = underperforming SPY (good for PUT).
    """
    return round(symbol_change - spy_change, 3)


# =============================================
# CONFLUENCE GRADE
# =============================================

def confluence_grade(breakout_strength, vol_ratio, vol_mult,
                     gap_pct, gap_direction, rs, direction,
                     et_hour):
    """
    Scores 0-100 across 5 factors, returns grade A/B/C/D and score.

    Factor weights:
      - Breakout strength  25pts  (how far past ORB)
      - Volume confirmation 20pts  (volume vs prior bar)
      - Gap alignment       20pts  (gap in same direction as trade)
      - Relative strength   20pts  (outperforming/underperforming SPY)
      - Time of day         15pts  (earlier = better for 0DTE)
    """
    pts = 0

    # 1. Breakout strength (0-25)
    # breakout_strength is pct as decimal e.g. 0.005 = 0.5%
    bs_pct = breakout_strength * 100
    if bs_pct >= 0.5:
        pts += 25
    elif bs_pct >= 0.3:
        pts += 18
    elif bs_pct >= 0.15:
        pts += 12
    else:
        pts += 6

    # 2. Volume ratio (0-20)
    if vol_ratio >= 2.0:
        pts += 20
    elif vol_ratio >= 1.5:
        pts += 15
    elif vol_ratio >= 1.2:
        pts += 10
    else:
        pts += 4

    # 3. Gap alignment (0-20)
    # Gap in same direction as trade = bullish confluence
    if direction == "CALL":
        if gap_direction == "UP" and gap_pct >= 0.5:
            pts += 20
        elif gap_direction == "UP":
            pts += 14
        elif gap_direction == "FLAT":
            pts += 8
        else:
            pts += 2   # gap against trade direction
    else:  # PUT
        if gap_direction == "DOWN" and abs(gap_pct) >= 0.5:
            pts += 20
        elif gap_direction == "DOWN":
            pts += 14
        elif gap_direction == "FLAT":
            pts += 8
        else:
            pts += 2

    # 4. Relative strength (0-20)
    if direction == "CALL":
        if rs >= 0.3:
            pts += 20
        elif rs >= 0.1:
            pts += 14
        elif rs >= -0.1:
            pts += 8
        else:
            pts += 2   # underperforming SPY on a CALL = bad
    else:  # PUT
        if rs <= -0.3:
            pts += 20
        elif rs <= -0.1:
            pts += 14
        elif rs <= 0.1:
            pts += 8
        else:
            pts += 2

    # 5. Time of day (0-15)
    # Best window: 9:30-11:00 AM ET (momentum window)
    # Decent: 11:00-1:00 PM
    # Risky: 1:00-2:00 PM
    # Late: 2:00+ PM (theta decay accelerates)
    if et_hour < 11:
        pts += 15
    elif et_hour < 13:
        pts += 10
    elif et_hour < 14:
        pts += 5
    else:
        pts += 1   # after 2pm, almost no value

    # Apply vol regime modifier
    pts = int(pts * vol_mult)
    pts = min(pts, 100)

    if pts >= 75:
        grade = "A"
        color = "#3fb950"   # green
    elif pts >= 55:
        grade = "B"
        color = "#e3b341"   # yellow
    elif pts >= 35:
        grade = "C"
        color = "#f0883e"   # orange
    else:
        grade = "D"
        color = "#f85149"   # red

    return grade, pts, color


# =============================================
# OPTIONS
# =============================================

def get_liquid_option(symbol, direction, underlying_price=None):
    """
    Fetch a real 0DTE ATM option via Tradier API.
    Steps:
      1. Get today expiration date from Tradier expirations endpoint
      2. Fetch full options chain for that expiration
      3. Filter to ATM strikes (within 2% of underlying)
      4. Select best by delta closest to 0.40
    Returns (premium, strike, is_live)
    """
    option_type = "call" if direction == "CALL" else "put"
    et          = pytz.timezone("America/New_York")
    today_str   = datetime.now(et).strftime("%Y-%m-%d")

    if not TRADIER_TOKEN:
        log("TRADIER_TOKEN not set - cannot fetch options")
        return None, None, False

    try:
        # Step 1: Get available expirations and confirm today is 0DTE
        exp_url = "{}/markets/options/expirations".format(TRADIER_URL)
        r = requests.get(exp_url, headers=TRADIER_HEADERS,
                         params={"symbol": symbol, "includeAllRoots": "true"},
                         timeout=10)
        log("Tradier expirations {}: HTTP {}".format(symbol, r.status_code))
        if r.status_code != 200:
            log("  Expirations error: {}".format(r.text[:150]))
            return None, None, False

        expirations = r.json().get("expirations", {}) or {}
        exp_dates   = expirations.get("date", [])
        if isinstance(exp_dates, str):
            exp_dates = [exp_dates]

        # Use today if available, else nearest expiration
        if today_str in exp_dates:
            target_exp = today_str
            log("  0DTE expiration found: {}".format(target_exp))
        elif exp_dates:
            target_exp = exp_dates[0]
            log("  No 0DTE today, using nearest: {}".format(target_exp))
        else:
            log("  No expirations available for {}".format(symbol))
            return None, None, False

        # Step 2: Fetch options chain for target expiration
        chain_url = "{}/markets/options/chains".format(TRADIER_URL)
        r2 = requests.get(chain_url, headers=TRADIER_HEADERS,
                          params={"symbol":     symbol,
                                  "expiration": target_exp,
                                  "greeks":     "true"},
                          timeout=10)
        log("Tradier chain {} {}: HTTP {}".format(symbol, target_exp, r2.status_code))
        if r2.status_code != 200:
            log("  Chain error: {}".format(r2.text[:150]))
            return None, None, False

        options = r2.json().get("options", {}) or {}
        chain   = options.get("option", [])
        if isinstance(chain, dict):
            chain = [chain]

        log("  Chain returned {} contracts".format(len(chain)))

        # Step 3: Filter to correct type and ATM strikes
        candidates = []
        for opt in chain:
            if opt.get("option_type", "").lower() != option_type[0]:
                continue  # wrong type

            strike = float(opt.get("strike", 0))
            if strike == 0:
                continue

            # Strike within 2% of underlying
            if underlying_price:
                pct_diff = abs(strike - underlying_price) / underlying_price
                if pct_diff > 0.02:
                    continue

            # Get mid price from bid/ask
            bid = float(opt.get("bid") or 0)
            ask = float(opt.get("ask") or 0)
            if bid > 0 and ask > 0:
                mid = round((bid + ask) / 2, 2)
            elif ask > 0:
                mid = ask
            else:
                continue

            # Realistic 0DTE premium range
            if not (0.05 <= mid <= 30.00):
                continue

            greeks = opt.get("greeks") or {}
            delta  = abs(float(greeks.get("delta") or 0))
            iv     = float(greeks.get("mid_iv") or 0)
            volume = int(opt.get("volume") or 0)
            oi     = int(opt.get("open_interest") or 0)

            candidates.append({
                "strike": strike,
                "price":  mid,
                "bid":    bid,
                "ask":    ask,
                "delta":  delta,
                "iv":     iv,
                "volume": volume,
                "oi":     oi,
            })

        log("  {} ATM candidates for {} {}".format(
            len(candidates), symbol, option_type))

        if not candidates:
            log("  No ATM candidates found - check strike range")
            return None, None, False

        # Step 4: Sort by closest delta to 0.40 (ATM sweet spot for 0DTE)
        candidates.sort(key=lambda x: abs(x["delta"] - 0.40))
        best = candidates[0]
        log("  Selected: strike={} delta={:.3f} bid={} ask={} mid={} vol={} oi={}".format(
            best["strike"], best["delta"], best["bid"], best["ask"],
            best["price"], best["volume"], best["oi"]))
        return best["price"], best["strike"], True

    except Exception as e:
        log("Tradier exception {}: {}".format(symbol, e))
        return None, None, False


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

def scan_all_symbols():
    results = []

    for symbol in SYMBOLS:
        result = {
            "symbol":    symbol,
            "direction": None,
            "score":     0,
            "grade":     None,
            "grade_pts": 0,
            "grade_color": "#8b949e",
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
            "vs_orb":    None,
            "vs_vwap":   None,
            "vol_ratio": None,
            "gap_pct":   None,
            "gap_dir":   None,
            "rs":        None,
            "spy_chg":   None,
            "late_entry": False,
        }

        intraday = get_intraday(symbol)
        daily    = get_daily(symbol)

        if not intraday or len(intraday) < ORB_BARS + 2 or not daily:
            result["status"] = "no data"
            results.append(result)
            continue

        # Volatility score (modifier, not hard block)
        vol_mult = volatility_score(daily)
        if vol_mult == 0.0:
            result["status"] = "dead market"
            results.append(result)
            continue

        # ORB using first 30 min (6 bars)
        orb      = intraday[:ORB_BARS]
        orb_high = max(b["h"] for b in orb)
        orb_low  = min(b["l"] for b in orb)
        current  = intraday[-1]
        price    = current["c"]
        vwap     = calculate_vwap(intraday)

        if not vwap:
            result["status"] = "no vwap"
            results.append(result)
            continue

        range_size = orb_high - orb_low
        vs_orb_high = round((price - orb_high) / orb_high * 100, 3)
        vs_orb_low  = round((orb_low - price) / orb_low * 100, 3)
        vs_vwap     = round((price - vwap) / vwap * 100, 3)

        orb_range = orb_high - orb_low

        # Gap analysis
        gap_pct, gap_dir = get_premarket_gap(daily, intraday)

        # Relative strength vs SPY
        spy_chg    = get_spy_change()
        sym_chg    = get_symbol_change(intraday)
        rs         = relative_strength(sym_chg, spy_chg)

        # Time of day
        et         = pytz.timezone("America/New_York")
        et_now     = datetime.now(et)
        et_hour    = et_now.hour + et_now.minute / 60.0
        late_entry = et_hour >= 14.0

        result["price"]          = round(price, 2)
        result["vwap"]           = round(vwap, 2)
        result["orb_high"]       = round(orb_high, 2)
        result["orb_low"]        = round(orb_low, 2)
        result["vol_mult"]       = round(vol_mult, 2)
        result["gap_pct"]        = gap_pct
        result["gap_dir"]        = gap_dir
        result["rs"]             = rs
        result["spy_chg"]        = spy_chg
        result["late_entry"]     = late_entry
        # Underlying price targets based on ORB range projection
        if orb_range > 0:
            result["und_call_t1"]   = round(orb_high + orb_range, 2)
            result["und_call_t2"]   = round(orb_high + orb_range * 2, 2)
            result["und_call_stop"] = round(orb_high - orb_range * 0.5, 2)
            result["und_put_t1"]    = round(orb_low  - orb_range, 2)
            result["und_put_t2"]    = round(orb_low  - orb_range * 2, 2)
            result["und_put_stop"]  = round(orb_low  + orb_range * 0.5, 2)

        # Determine direction and breakout strength
        direction         = None
        breakout_strength = 0

        if price > orb_high and price > vwap:
            direction         = "CALL"
            breakout_strength = (price - orb_high) / orb_high
            result["vs_orb"]  = "+{}%".format(abs(vs_orb_high))
            result["vs_vwap"] = "+{}%".format(abs(vs_vwap))

        elif price < orb_low and price < vwap:
            direction         = "PUT"
            breakout_strength = (orb_low - price) / orb_low
            result["vs_orb"]  = "-{}%".format(abs(vs_orb_low))
            result["vs_vwap"] = "-{}%".format(abs(vs_vwap))

        else:
            # No confirmed breakout yet - classify as WATCHING
            # Show best directional bias based on price location
            if price > vwap:
                result["direction"] = "CALL"
                result["vs_vwap"]   = "+{}%".format(abs(vs_vwap))
                result["vs_orb"]    = "{:.2f}% from ORB high".format(
                    abs(vs_orb_high))
            else:
                result["direction"] = "PUT"
                result["vs_vwap"]   = "-{}%".format(abs(vs_vwap))
                result["vs_orb"]    = "{:.2f}% from ORB low".format(
                    abs(vs_orb_low))

            # Score based on proximity to breakout level
            proximity = 1 - min(abs(vs_orb_high), abs(vs_orb_low)) / 100
            vol_ratio = current["v"] / intraday[-2]["v"] if intraday[-2]["v"] > 0 else 1
            result["score"]  = round(proximity * vol_mult * 10, 2)
            result["status"] = "WATCHING"
            results.append(result)
            continue

        # Confirmed breakout - get options
        vol_ratio = current["v"] / intraday[-2]["v"] if intraday[-2]["v"] > 0 else 1
        score     = (breakout_strength * 100 + vol_ratio) * vol_mult

        # Confluence grade
        grade, grade_pts, grade_color = confluence_grade(
            breakout_strength, vol_ratio, vol_mult,
            gap_pct, gap_dir, rs, direction, et_hour)

        premium, strike, is_live = get_liquid_option(symbol, direction, price)

        if premium and is_live:
            contracts, stop, target = calculate_contracts(premium, score)
            result["premium"]   = round(premium, 2)
            result["strike"]    = strike
            result["contracts"] = contracts
            result["stop"]      = stop
            result["target"]    = target
            result["is_live"]   = True
            result["status"]    = "SIGNAL"
        else:
            result["is_live"]   = False
            result["status"]    = "SIGNAL (no options)"

        result["direction"]   = direction
        result["score"]       = round(score, 2)
        result["grade"]       = grade
        result["grade_pts"]   = grade_pts
        result["grade_color"] = grade_color
        results.append(result)
        log("{}: {} {} grade={} ({}) score={:.2f}".format(
            symbol, result["status"], direction, grade, grade_pts, score))

    # Sort: SIGNAL first, then WATCHING, then rest - all by score desc
    def sort_key(r):
        s = r.get("status","")
        if s == "SIGNAL":
            return (0, -r.get("score",0))
        elif s == "WATCHING":
            return (1, -r.get("score",0))
        elif "SIGNAL" in s:
            return (2, -r.get("score",0))
        else:
            return (3, 0)

    results.sort(key=sort_key)
    return results


# =============================================
# MAIN SCAN RUNNER
# =============================================

def run_signal_scan():
    global all_signals, next_scan_at
    log("=== Running signal scan ===")
    log("Key set: {} | Secret set: {} | Bot: {}".format(
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

    signals  = [r for r in results if r["status"] == "SIGNAL"]
    watching = [r for r in results if r["status"] == "WATCHING"]

    # Telegram: alert on confirmed signals
    for sig in signals:
        if bot_enabled and should_alert(sig["symbol"], sig["direction"]):
            db_log_signal(sig)
            msg = (
                "INSTITUTIONAL BREAKOUT\n\n"
                "Symbol: {}\nDirection: {}\nScore: {}\n\n"
                "Underlying: ${}\nStrike: {}\nPremium: ${}\n\n"
                "Contracts: {}\nStop: ${}\nTarget: ${}\n\n"
                "Vol Multiplier: {}x"
            ).format(
                sig["symbol"], sig["direction"], sig["score"],
                sig["price"], sig["strike"], sig["premium"],
                sig["contracts"], sig["stop"], sig["target"],
                sig.get("vol_mult", 1.0)
            )
            send_telegram(msg)
            break  # Only alert best signal

    # Telegram: send watching list if no signals
    if not signals and watching and bot_enabled:
        et    = pytz.timezone("America/New_York")
        now   = datetime.now(et)
        # Only send watching alert once, between 10:00-10:05 AM
        if now.hour == 10 and now.minute < 6:
            top3  = watching[:3]
            lines = []
            for w in top3:
                lines.append("{} {} | Score:{} | {}ORB | {}VWAP".format(
                    w["symbol"], w.get("direction","?"),
                    w.get("score","?"),
                    w.get("vs_orb","?"), w.get("vs_vwap","?")))
            send_telegram(
                "WATCHING (no confirmed breakouts yet):\n\n" +
                "\n".join(lines) +
                "\n\nWaiting for ORB breakout + volume confirmation."
            )

    log("Scan done: {} SIGNAL, {} WATCHING, {} other".format(
        len(signals), len(watching),
        len(results) - len(signals) - len(watching)))


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
                    log("Telegram command: {}".format(text))
                    handle_telegram_command(text)
        except Exception as e:
            log("Telegram poller error: {}".format(e))
        time.sleep(3)


# =============================================
# DASHBOARD
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

    is_open       = market_open()
    market_color  = "green" if is_open else "red"
    market_status = "OPEN" if is_open else "CLOSED"
    pnl_color     = "green" if total_pnl >= 0 else "red"

    signal_rows = ""
    for s in signals:
        status = s.get("status", "")
        sym    = s["symbol"]
        price  = s.get("price", "-")
        score  = s.get("score", 0)
        d      = s.get("direction") or ""
        dcolor = "green" if d == "CALL" else "red"

        if status == "SIGNAL":
            # Build underlying target string based on direction
            if d == "CALL":
                und_tgt_str = "T1:${} T2:${}  Stop:${}".format(
                    s.get("und_call_t1","-"),
                    s.get("und_call_t2","-"),
                    s.get("und_call_stop","-"))
            else:
                und_tgt_str = "T1:${} T2:${}  Stop:${}".format(
                    s.get("und_put_t1","-"),
                    s.get("und_put_t2","-"),
                    s.get("und_put_stop","-"))

            grade       = s.get("grade") or "-"
            grade_pts   = s.get("grade_pts") or 0
            grade_color = s.get("grade_color") or "#8b949e"
            gap_pct     = s.get("gap_pct") or 0
            gap_dir     = s.get("gap_dir") or "-"
            rs          = s.get("rs") or 0
            late        = s.get("late_entry", False)

            # Gap display
            gap_sign  = "+" if gap_pct >= 0 else ""
            gap_color = "#3fb950" if gap_dir == "UP" else "#f85149" if gap_dir == "DOWN" else "#8b949e"
            rs_color  = "#3fb950" if rs >= 0 else "#f85149"

            # Late entry warning
            late_badge = ("<span style='background:#9e6a03;color:white;padding:1px 4px;"
                          "border-radius:3px;font-size:9px;margin-left:4px'>LATE</span>"
                          if late else "")

            signal_rows += (
                "<tr style='border-bottom:1px solid #21262d;background:#0d2818'>"
                # Symbol
                "<td style='padding:8px'><b>{sym}</b>{late}</td>"
                # Direction
                "<td style='color:{dc};padding:8px'><b>{d}</b></td>"
                # Grade (replaces raw score)
                "<td style='padding:8px;text-align:center'>"
                "<div style='font-size:22px;font-weight:bold;color:{gc}'>{grade}</div>"
                "<div style='font-size:10px;color:#8b949e'>{gpts}pts</div>"
                "</td>"
                # Price + underlying targets
                "<td style='padding:8px'>"
                "<div style='font-size:13px'><b>${price}</b></div>"
                "<div style='font-size:10px;color:#3fb950;margin-top:2px'>{utgt}</div>"
                "</td>"
                # Gap + RS
                "<td style='padding:8px;font-size:11px'>"
                "<div>Gap: <span style='color:{gapc}'>{gsign}{gpct}%</span></div>"
                "<div>RS: <span style='color:{rsc}'>{rs:+.2f}%</span></div>"
                "</td>"
                # Premium
                "<td style='padding:8px'>"
                "<div style='font-size:13px'>${prem} "
                "<span style='background:#238636;color:white;padding:1px 4px;"
                "border-radius:3px;font-size:9px'>LIVE</span></div>"
                "<div style='font-size:10px;color:#8b949e;margin-top:2px'>"
                "Stop:${stp} Tgt:${tgt}</div>"
                "</td>"
                # Action
                "<td style='padding:8px'>"
                "<span style='background:#1f6feb;color:white;padding:2px 6px;"
                "border-radius:4px;font-size:10px'>SIGNAL</span>&nbsp;"
                "<a href='/take?sym={sym}&dir={d}&prem={prem}&con={con}"
                "&stp={stp}&tgt={tgt}&grade={grade}&gpts={gpts}"
                "&gap={gpct}&gdir={gdir}&rs={rs:.2f}' "
                "style='background:#238636;color:white;padding:4px 8px;"
                "border-radius:5px;text-decoration:none;font-size:11px'>TAKE</a>"
                "</td></tr>"
            ).format(
                sym=sym, late=late_badge, dc=dcolor, d=d,
                grade=grade, gpts=grade_pts, gc=grade_color,
                price=price, utgt=und_tgt_str,
                gapc=gap_color, gsign=gap_sign, gpct=round(abs(gap_pct),2),
                gdir=gap_dir,
                rsc=rs_color, rs=rs,
                prem=s.get("premium","-"),
                stp=s.get("stop","-"), tgt=s.get("target","-"),
                con=s.get("contracts","1")
            )

        elif status == "WATCHING":
            # Show breakout trigger level for WATCHING symbols
            if d == "CALL":
                trigger = "Break>${}".format(s.get("orb_high","-"))
                t1_str  = "T1:${}".format(s.get("und_call_t1","-"))
            else:
                trigger = "Break<${}".format(s.get("orb_low","-"))
                t1_str  = "T1:${}".format(s.get("und_put_t1","-"))
            signal_rows += (
                "<tr style='border-bottom:1px solid #21262d'>"
                "<td style='padding:8px'><b>{sym}</b></td>"
                "<td style='color:{dc};padding:8px'>{d}</td>"
                "<td style='padding:8px'>"
                "<div style='font-size:13px'>${price}</div>"
                "<div style='font-size:10px;color:#e3b341;margin-top:2px'>"
                "{trigger}</div>"
                "</td>"
                "<td style='padding:8px'>{score}</td>"
                "<td style='padding:8px;font-size:11px;color:#8b949e'>"
                "{vs_orb}</td>"
                "<td style='padding:8px;font-size:11px;color:#8b949e'>"
                "{t1}</td>"
                "<td style='padding:8px;font-size:11px;color:#8b949e'>"
                "Vol {vm}x</td>"
                "<td style='padding:8px'>"
                "<span style='background:#9e6a03;color:white;padding:2px 6px;"
                "border-radius:4px;font-size:10px'>WATCH</span>"
                "</td></tr>"
            ).format(
                sym=sym, dc=dcolor, d=d, price=price, score=score,
                trigger=trigger, t1=t1_str,
                vs_orb=s.get("vs_orb","-"),
                vm=s.get("vol_mult","-")
            )

        elif "SIGNAL" in status:
            signal_rows += (
                "<tr style='border-bottom:1px solid #21262d;opacity:0.8'>"
                "<td style='padding:8px'><b>{}</b></td>"
                "<td style='color:{};padding:8px'>{}</td>"
                "<td style='padding:8px'>${}</td>"
                "<td style='padding:8px'>{}</td>"
                "<td colspan='3' style='padding:8px;color:#e3b341'>"
                "Breakout confirmed - no option data</td>"
                "<td></td></tr>"
            ).format(sym, dcolor, d, price, score)

        else:
            signal_rows += (
                "<tr style='border-bottom:1px solid #21262d;opacity:0.35'>"
                "<td style='padding:8px'>{}</td>"
                "<td colspan='7' style='padding:8px;color:#8b949e'>{}</td>"
                "</tr>"
            ).format(sym, status)

    open_rows = ""
    for t in open_trades:
        cp = get_current_price(t["symbol"])
        if cp and t["premium"]:
            unreal = round((cp - t["premium"]) * 100 * t["contracts"], 2)
            uc     = "green" if unreal >= 0 else "red"
            us     = "<span style='color:{}'>${}</span>".format(uc, unreal)
        else:
            us = "<span style='color:#8b949e'>-</span>"
        open_rows += (
            "<tr style='border-bottom:1px solid #21262d'>"
            "<td style='padding:8px'>{}</td><td style='padding:8px'>{}</td>"
            "<td style='padding:8px'>${}</td><td style='padding:8px'>{}x</td>"
            "<td style='padding:8px'>{}</td>"
            "<td style='padding:8px'>"
            "<a href='/close?id={}&outcome=WIN&exit={}' "
            "style='background:#238636;color:white;padding:4px 8px;"
            "border-radius:4px;text-decoration:none;font-size:11px;margin-right:4px'>WIN</a>"
            "<a href='/close?id={}&outcome=LOSS&exit={}' "
            "style='background:#da3633;color:white;padding:4px 8px;"
            "border-radius:4px;text-decoration:none;font-size:11px'>LOSS</a>"
            "</td></tr>"
        ).format(t["symbol"], t["direction"], t["premium"], t["contracts"], us,
                 t["id"], cp or 0, t["id"], cp or 0)

    closed_rows = ""
    for t in closed:
        pc = "green" if (t["pnl"] or 0) >= 0 else "red"
        closed_rows += (
            "<tr style='border-bottom:1px solid #21262d'>"
            "<td style='padding:8px'>{}</td><td style='padding:8px'>{}</td>"
            "<td style='padding:8px'>${}</td><td style='padding:8px'>{}</td>"
            "<td style='padding:8px;color:{}'>${}</td></tr>"
        ).format(t["symbol"], t["direction"], t["premium"],
                 t["outcome"], pc, t["pnl"] or 0)

    html = (
        "<!DOCTYPE html><html><head>"
        "<meta http-equiv='refresh' content='30'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<style>"
        "body{{background:#0d1117;color:white;font-family:Arial,sans-serif;"
        "padding:15px;margin:0}}"
        "h1{{font-size:18px;margin-bottom:5px}}"
        ".card{{background:#161b22;border-radius:10px;margin-bottom:15px;overflow:hidden}}"
        ".ch{{padding:12px 15px;border-bottom:1px solid #21262d;"
        "display:flex;justify-content:space-between;align-items:center}}"
        ".green{{color:#3fb950}}.red{{color:#f85149}}.yellow{{color:#e3b341}}"
        "table{{width:100%;border-collapse:collapse;font-size:12px}}"
        "th{{padding:8px;text-align:left;color:#8b949e;border-bottom:1px solid #21262d}}"
        ".debug{{background:#0a0d12;padding:12px;border-radius:8px;font-size:10px;"
        "font-family:monospace;max-height:200px;overflow-y:auto;color:#8b949e}}"
        ".sr{{display:flex;gap:8px;margin-bottom:15px}}"
        ".st{{background:#161b22;border-radius:8px;padding:12px;flex:1;text-align:center}}"
        ".sv{{font-size:20px;font-weight:bold}}"
        ".sl{{font-size:10px;color:#8b949e;margin-top:3px}}"
        "a.nav{{color:#58a6ff;text-decoration:none;font-size:12px;margin-right:8px}}"
        "</style></head><body>"
        "<h1>Institutional 0DTE Engine</h1>"
        "<div style='margin-bottom:10px;font-size:12px;color:#8b949e'>"
        "Market:<span class='{mc}'> {ms}</span> | "
        "Scan:{sc}s | "
        "Bot:<span class='{bc}'> {be}</span> | "
        "<a class='nav' href='/stats'>Stats</a>"
        "<a class='nav' href='/alpaca-test'>Alpaca</a>"
        "<a class='nav' href='/telegram-test'>Telegram</a>"
        "<a class='nav' href='/debug'>Debug</a>"
        "</div>"
        "<div class='sr'>"
        "<div class='st'><div class='sv {pc}'>${pl}</div>"
        "<div class='sl'>Today P&amp;L</div></div>"
        "<div class='st'><div class='sv'>{nt}</div>"
        "<div class='sl'>Trades</div></div>"
        "<div class='st'><div class='sv green'>{nw}</div>"
        "<div class='sl'>Wins</div></div>"
        "<div class='st'><div class='sv red'>{nl}</div>"
        "<div class='sl'>Losses</div></div>"
        "</div>"
        "<div class='card'>"
        "<div class='ch'><span>Signal Scanner</span>"
        "<span style='font-size:11px;color:#8b949e'>"
        "{ns} symbols | ORB=30min | Vol-adjusted</span></div>"
        "<table><tr>"
        "<th>Symbol</th><th>Dir</th><th style='text-align:center'>Grade</th>"
        "<th>Price / Targets</th><th>Gap / RS</th>"
        "<th>Premium</th><th>Action</th>"
        "</tr>{sr2}</table></div>"
        "<div class='card'><div class='ch'><span>Open Trades</span></div>"
        "<table><tr><th>Symbol</th><th>Dir</th><th>Entry</th>"
        "<th>Size</th><th>Unreal P&amp;L</th><th>Close</th></tr>"
        "{or_}</table></div>"
        "<div class='card'><div class='ch'><span>Today Closed</span></div>"
        "<table><tr><th>Symbol</th><th>Dir</th><th>Entry</th>"
        "<th>Result</th><th>P&amp;L</th></tr>"
        "{cr}</table></div>"
        "<div class='card' style='padding:12px'>"
        "<div style='color:#8b949e;font-size:11px;margin-bottom:6px'>Debug Log</div>"
        "<div class='debug'>{ll}</div></div>"
        "</body></html>"
    ).format(
        mc=market_color, ms=market_status, sc=secs,
        bc="green" if bot_enabled else "red",
        be="ON" if bot_enabled else "PAUSED",
        pc=pnl_color, pl=round(total_pnl,2),
        nt=len(closed), nw=wins, nl=losses,
        ns=len(signals),
        sr2=signal_rows or (
            "<tr><td colspan='8' style='padding:15px;color:#8b949e;"
            "text-align:center'>Waiting for scan...</td></tr>"),
        or_=open_rows or (
            "<tr><td colspan='6' style='padding:15px;color:#8b949e;"
            "text-align:center'>No open trades</td></tr>"),
        cr=closed_rows or (
            "<tr><td colspan='5' style='padding:15px;color:#8b949e;"
            "text-align:center'>No closed trades today</td></tr>"),
        ll="<br>".join(logs) if logs else "No logs yet"
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
    sym   = request.args.get("sym", "")
    dir_  = request.args.get("dir", "")
    prem  = request.args.get("prem", "0")
    con   = request.args.get("con", "1")
    stp   = request.args.get("stp", "0")
    tgt   = request.args.get("tgt", "0")
    grade = request.args.get("grade", None)
    gpts  = request.args.get("gpts", None)
    gap   = request.args.get("gap", None)
    gdir  = request.args.get("gdir", None)
    rs    = request.args.get("rs", None)
    try:
        db_log_trade(
            sym, dir_, float(prem), int(con), float(stp), float(tgt),
            grade=grade,
            grade_pts=int(gpts) if gpts else None,
            gap_pct=float(gap) if gap else None,
            gap_dir=gdir,
            rs=float(rs) if rs else None
        )
        log("Trade taken: {} {} {} grade={} prem={}".format(
            sym, dir_, grade, gpts, prem))
        send_telegram(
            "TRADE TAKEN\n{} {} | Grade: {} ({}pts)\n"
            "Entry: ${} | {}x | Stop: ${} | Target: ${}\n"
            "Gap: {}% {} | RS vs SPY: {}%".format(
                sym, dir_, grade or "?", gpts or "?",
                prem, con, stp, tgt,
                gap or "?", gdir or "?", rs or "?"))
    except Exception as e:
        log("Take trade error: {}".format(e))
    return redirect("/")


@app.route("/close")
def close_trade():
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


@app.route("/stats")
def stats_page():
    """Win rate breakdown by symbol, grade, hour, direction."""
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        c.execute("""
            SELECT symbol, direction, outcome, pnl, r_mult,
                   grade, grade_pts, gap_pct, gap_dir, rs, entry_hour, ts
            FROM trades WHERE outcome != 'OPEN'
            ORDER BY ts DESC
        """)
        rows = c.fetchall()
        conn.close()
    except Exception as e:
        return "DB error: {}".format(e)

    trades = []
    for r in rows:
        trades.append({
            "symbol": r[0], "direction": r[1], "outcome": r[2],
            "pnl": r[3] or 0, "r_mult": r[4] or 0,
            "grade": r[5] or "?", "grade_pts": r[6] or 0,
            "gap_pct": r[7] or 0, "gap_dir": r[8] or "?",
            "rs": r[9] or 0, "entry_hour": r[10] or 0, "ts": r[11]
        })

    if not trades:
        return ("<html><body style='background:#0d1117;color:white;"
                "font-family:Arial;padding:20px'>"
                "<h2>No closed trades yet</h2>"
                "<a href='/' style='color:#58a6ff'>Back to dashboard</a>"
                "</body></html>")

    total  = len(trades)
    wins   = len([t for t in trades if t["outcome"] == "WIN"])
    losses = len([t for t in trades if t["outcome"] == "LOSS"])
    wr     = round(wins / total * 100, 1) if total else 0
    total_pnl = round(sum(t["pnl"] for t in trades), 2)
    avg_r  = round(sum(t["r_mult"] for t in trades) / total, 2) if total else 0

    def stat_rows(group_key, label):
        groups = {}
        for t in trades:
            k = str(t.get(group_key, "?"))
            if k not in groups:
                groups[k] = []
            groups[k].append(t)
        rows_html = ""
        for k in sorted(groups.keys()):
            g   = groups[k]
            gw  = len([x for x in g if x["outcome"] == "WIN"])
            gl  = len(g) - gw
            gwr = round(gw / len(g) * 100, 1)
            gpnl = round(sum(x["pnl"] for x in g), 2)
            pc  = "#3fb950" if gpnl >= 0 else "#f85149"
            wrc = "#3fb950" if gwr >= 55 else "#e3b341" if gwr >= 45 else "#f85149"
            rows_html += (
                "<tr style='border-bottom:1px solid #21262d'>"
                "<td style='padding:8px'>{}</td>"
                "<td style='padding:8px'>{}</td>"
                "<td style='padding:8px;color:{}'>{:.0f}%</td>"
                "<td style='padding:8px'>{}/{}</td>"
                "<td style='padding:8px;color:{}'>${}</td>"
                "</tr>"
            ).format(k, len(g), wrc, gwr, gw, gl, pc, gpnl)
        return rows_html

    def hour_label(h):
        if h < 10:   return "9:30-10:00"
        elif h < 11: return "10:00-11:00"
        elif h < 12: return "11:00-12:00"
        elif h < 13: return "12:00-1:00"
        elif h < 14: return "1:00-2:00"
        else:        return "2:00+ LATE"

    # Group by hour bucket
    hour_groups = {}
    for t in trades:
        k = hour_label(t["entry_hour"])
        if k not in hour_groups: hour_groups[k] = []
        hour_groups[k].append(t)

    hour_rows = ""
    for k in ["9:30-10:00","10:00-11:00","11:00-12:00",
               "12:00-1:00","1:00-2:00","2:00+ LATE"]:
        if k not in hour_groups: continue
        g   = hour_groups[k]
        gw  = len([x for x in g if x["outcome"] == "WIN"])
        gwr = round(gw / len(g) * 100, 1)
        gpnl = round(sum(x["pnl"] for x in g), 2)
        pc  = "#3fb950" if gpnl >= 0 else "#f85149"
        wrc = "#3fb950" if gwr >= 55 else "#e3b341" if gwr >= 45 else "#f85149"
        hour_rows += (
            "<tr style='border-bottom:1px solid #21262d'>"
            "<td style='padding:8px'>{}</td>"
            "<td style='padding:8px'>{}</td>"
            "<td style='padding:8px;color:{}'>{:.0f}%</td>"
            "<td style='padding:8px;color:{}'>${}</td>"
            "</tr>"
        ).format(k, len(g), wrc, gwr, pc, gpnl)

    html = """<!DOCTYPE html><html><head>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<style>
body{{background:#0d1117;color:white;font-family:Arial,sans-serif;padding:15px;margin:0}}
h2{{font-size:16px;margin:20px 0 8px 0;color:#58a6ff}}
.card{{background:#161b22;border-radius:10px;margin-bottom:15px;overflow:hidden}}
.ch{{padding:10px 15px;border-bottom:1px solid #21262d;font-size:13px;font-weight:bold}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{padding:8px;text-align:left;color:#8b949e;border-bottom:1px solid #21262d}}
.sr{{display:flex;gap:8px;margin-bottom:15px;flex-wrap:wrap}}
.st{{background:#161b22;border-radius:8px;padding:12px;flex:1;min-width:80px;text-align:center}}
.sv{{font-size:20px;font-weight:bold}}
.sl{{font-size:10px;color:#8b949e;margin-top:3px}}
a.nav{{color:#58a6ff;text-decoration:none;font-size:12px}}
</style></head><body>
<div style='margin-bottom:12px'>
<a class='nav' href='/'>&#8592; Dashboard</a>
</div>
<h1 style='font-size:18px;margin-bottom:5px'>Trade Statistics</h1>
<div style='font-size:12px;color:#8b949e;margin-bottom:15px'>{total} closed trades</div>

<div class='sr'>
<div class='st'><div class='sv {wr_c}'>{wr}%</div><div class='sl'>Win Rate</div></div>
<div class='st'><div class='sv'>{total}</div><div class='sl'>Trades</div></div>
<div class='st'><div class='sv' style='color:#3fb950'>{wins}</div><div class='sl'>Wins</div></div>
<div class='st'><div class='sv' style='color:#f85149'>{losses}</div><div class='sl'>Losses</div></div>
<div class='st'><div class='sv {pnl_c}'>${total_pnl}</div><div class='sl'>Total P&L</div></div>
<div class='st'><div class='sv'>{avg_r}R</div><div class='sl'>Avg R</div></div>
</div>

<div class='card'>
<div class='ch'>By Symbol</div>
<table><tr><th>Symbol</th><th>Trades</th><th>Win%</th><th>W/L</th><th>P&L</th></tr>
{sym_rows}</table></div>

<div class='card'>
<div class='ch'>By Grade</div>
<table><tr><th>Grade</th><th>Trades</th><th>Win%</th><th>W/L</th><th>P&L</th></tr>
{grade_rows}</table></div>

<div class='card'>
<div class='ch'>By Direction</div>
<table><tr><th>Direction</th><th>Trades</th><th>Win%</th><th>W/L</th><th>P&L</th></tr>
{dir_rows}</table></div>

<div class='card'>
<div class='ch'>By Time of Day</div>
<table><tr><th>Window</th><th>Trades</th><th>Win%</th><th>P&L</th></tr>
{hour_rows}</table></div>

<div class='card'>
<div class='ch'>Recent Trades</div>
<table><tr><th>Symbol</th><th>Dir</th><th>Grade</th><th>Gap</th><th>RS</th><th>Result</th><th>P&L</th></tr>
{recent_rows}</table></div>

</body></html>""".format(
        total=total, wins=wins, losses=losses, wr=wr,
        wr_c="green" if wr >= 50 else "red",
        total_pnl=total_pnl,
        pnl_c="color:#3fb950" if total_pnl >= 0 else "color:#f85149",
        avg_r=avg_r,
        sym_rows=stat_rows("symbol", "Symbol"),
        grade_rows=stat_rows("grade", "Grade"),
        dir_rows=stat_rows("direction", "Direction"),
        hour_rows=hour_rows,
        recent_rows="".join([
            "<tr style='border-bottom:1px solid #21262d'>"
            "<td style='padding:8px'>{}</td>"
            "<td style='padding:8px'>{}</td>"
            "<td style='padding:8px;font-weight:bold;color:{}'>{}</td>"
            "<td style='padding:8px;font-size:11px;color:{}'>{:+.2f}%</td>"
            "<td style='padding:8px;font-size:11px;color:{}'>{:+.2f}%</td>"
            "<td style='padding:8px;color:{}'>{}</td>"
            "<td style='padding:8px;color:{}'>${}</td>"
            "</tr>".format(
                t["symbol"], t["direction"],
                "#3fb950" if t["grade"]=="A" else "#e3b341" if t["grade"]=="B" else "#f0883e" if t["grade"]=="C" else "#8b949e",
                t["grade"],
                "#3fb950" if t["gap_pct"]>=0 else "#f85149", t["gap_pct"],
                "#3fb950" if t["rs"]>=0 else "#f85149", t["rs"],
                "#3fb950" if t["outcome"]=="WIN" else "#f85149", t["outcome"],
                "#3fb950" if t["pnl"]>=0 else "#f85149", round(t["pnl"],2)
            ) for t in trades[:20]
        ])
    )
    return html


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
    return jsonify(results)


@app.route("/tradier-test")
def tradier_test():
    """Test Tradier options data for SPY - shows live chain."""
    results = {"token_set": bool(TRADIER_TOKEN)}
    et        = pytz.timezone("America/New_York")
    today_str = datetime.now(et).strftime("%Y-%m-%d")
    try:
        r = requests.get("{}/markets/options/expirations".format(TRADIER_URL),
                         headers=TRADIER_HEADERS,
                         params={"symbol": "SPY", "includeAllRoots": "true"},
                         timeout=10)
        results["expirations"] = {"status": r.status_code,
                                   "body": r.json() if r.status_code==200 else r.text[:300]}
    except Exception as e:
        results["expirations"] = {"error": str(e)}
    try:
        r2 = requests.get("{}/markets/options/chains".format(TRADIER_URL),
                          headers=TRADIER_HEADERS,
                          params={"symbol": "SPY", "expiration": today_str,
                                  "greeks": "true"},
                          timeout=10)
        body = r2.json() if r2.status_code == 200 else r2.text[:500]
        # Trim chain to first 5 ATM contracts only for readability
        if r2.status_code == 200:
            chain = (body.get("options") or {}).get("option", [])
            if isinstance(chain, dict):
                chain = [chain]
            results["chain_total"]  = len(chain)
            results["chain_sample"] = chain[:5]
        else:
            results["chain_error"]  = body
    except Exception as e:
        results["chain"] = {"error": str(e)}
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


@app.route("/token-check")
def token_check():
    """Diagnoses the exact Telegram token format issue."""
    raw_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    result = {
        "raw_length":       len(raw_token),
        "stripped_length":  len(raw_token.strip()),
        "colon_count":      raw_token.count(":"),
        "has_leading_space": raw_token != raw_token.lstrip(),
        "has_trailing_space": raw_token != raw_token.rstrip(),
        "first_10_chars":   repr(raw_token[:10]),
        "last_10_chars":    repr(raw_token[-10:]),
        "chat_id":          os.getenv("TELEGRAM_CHAT_ID",""),
    }
    token = raw_token.strip()
    if ":" in token:
        parts = token.split(":", 1)
        result["bot_id"]       = parts[0]
        result["bot_id_valid"] = parts[0].isdigit()
        result["hash_length"]  = len(parts[1])
    # Try getMe to verify token with Telegram
    try:
        r = requests.get(
            "https://api.telegram.org/bot{}/getMe".format(token),
            timeout=5)
        result["getMe_status"] = r.status_code
        result["getMe_body"]   = r.json()
    except Exception as e:
        result["getMe_error"] = str(e)
    return jsonify(result)


# =============================================
# STARTUP
# =============================================

init_db()
threading.Thread(target=background_scheduler, daemon=True).start()
threading.Thread(target=telegram_poller,      daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
