from flask import Flask, jsonify, render_template_string, request, redirect
import requests
import os
import statistics
import math
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

# Broader universe for swing scanner - liquid, optionable stocks
SWING_UNIVERSE = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD", "AVGO", "CRM",
    # Semis & hardware
    "INTC", "MU", "QCOM", "TXN", "AMAT", "LRCX", "KLAC", "MRVL", "ON", "SMCI",
    # Finance
    "JPM", "GS", "MS", "BAC", "C", "V", "MA", "AXP", "BX", "KKR",
    # Healthcare & biotech
    "LLY", "UNH", "ABBV", "MRK", "PFE", "JNJ", "GILD", "REGN", "BIIB", "MRNA",
    # Energy & industrials
    "XOM", "CVX", "OXY", "SLB", "CAT", "DE", "HON", "RTX", "LMT", "GE",
    # Consumer & retail
    "COST", "WMT", "HD", "NKE", "SBUX", "MCD", "TGT", "LULU", "DECK", "RH",
    # ETFs with strong options
    "SPY", "QQQ", "IWM", "XLK", "XLF", "GLD", "SLV", "ARKK",
]

ALPACA_KEY    = os.getenv("APCA_API_KEY_ID", "").strip()
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY", "").strip()

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET
}

DATA_URL  = "https://data.alpaca.markets/v2/stocks/{}/bars"
QUOTE_URL = "https://data.alpaca.markets/v2/stocks/{}/quotes/latest"
CLOCK_URL = "https://paper-api.alpaca.markets/v2/clock"

ALERT_FILE     = "/tmp/last_alert.json"
DB_FILE        = "/tmp/trades.db"
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "").strip()

state_lock   = threading.Lock()
debug_log    = []
all_signals  = []
next_scan_at = 0
bot_enabled  = True

swing_signals    = []
next_swing_scan  = 0


# =============================================
# SCANNER CONFIG  (AI-tunable parameters)
# =============================================

DEFAULT_CONFIG = {
    "version":        1,
    "updated_at":     "default",
    "updated_by":     "default",

    # --- Confluence grade weights (should sum to ~100) ---
    "weight_breakout": 25,
    "weight_volume":   20,
    "weight_gap":      20,
    "weight_rs":       20,
    "weight_time":     15,

    # --- Grade thresholds ---
    "grade_a_min": 75,
    "grade_b_min": 55,
    "grade_c_min": 35,

    # --- Breakout strength % thresholds ---
    "bs_strong": 0.50,
    "bs_medium": 0.30,
    "bs_weak":   0.15,

    # --- Volume ratio thresholds ---
    "vol_high": 2.0,
    "vol_med":  1.5,
    "vol_low":  1.2,

    # --- Time of day (ET hour) ---
    "time_prime_end":  11.0,
    "time_decent_end": 13.0,
    "time_risky_end":  14.0,
    "late_entry_hour": 14.0,

    # --- Rank score bonuses/penalties ---
    "rank_align_bonus":    20,
    "rank_align_penalty":  -15,
    "rank_trend_full":     20,
    "rank_trend_partial":  10,
    "rank_trend_oppose":   -10,
    "rank_vol_exceptional": 15,
    "rank_vol_elevated":    8,
    "rank_vol_light":       -10,
    "rank_clear_t2":        20,
    "rank_clear_t1":        10,
    "rank_blocked":         -15,
    "rank_late_penalty":    -20,

    # --- VWAP reclaim strategy ---
    "vwap_reclaim_enabled":     True,
    "vwap_reclaim_vol_min":     1.3,
    "vwap_reclaim_lookback":    6,

    # --- VWAP trend strategy (Zarattini & Aziz SSRN 2023) ---
    "vwap_trend_enabled":       True,
    "vwap_trend_min_bars":      8,
    "vwap_trend_min_dist_pct":  0.15,
    "vwap_trend_vol_min":       0.9,
    "vwap_trend_trend_pct":     0.55,

    # --- VWAP mean reversion strategy ---
    "vwap_mr_enabled":          True,
    "vwap_mr_band_std":         2.0,
    "vwap_mr_min_bars":         12,
    "vwap_mr_vol_min":          0.8,

    # --- Initial Balance extension strategy (Market Profile) ---
    "ib_ext_enabled":           True,
    "ib_ext_multiplier":        1.0,
    "ib_ext_min_hour":          10.5,
    "ib_ext_vol_min":           1.2,

    # --- Filter strictness ---
    "counter_trend_allowed":    True,
    "min_grade":                "C",

    # --- AI state ---
    "ai_insight": "Baseline config -- collecting trade data to begin optimization.",
    "ai_focus":   "Trade all A/B/C grade signals and log outcomes to build the dataset.",
    "ai_version": 0,
}

# Live config -- loaded from DB on startup, updated by AI
_scanner_config = dict(DEFAULT_CONFIG)
_config_lock    = threading.Lock()

def get_config():
    with _config_lock:
        return dict(_scanner_config)

def update_config(new_values, updated_by="ai"):
    with _config_lock:
        _scanner_config.update(new_values)
        _scanner_config["updated_by"] = updated_by
        et = pytz.timezone("America/New_York")
        _scanner_config["updated_at"] = datetime.now(et).strftime("%Y-%m-%d %H:%M")
        _scanner_config["ai_version"] = _scanner_config.get("ai_version", 0) + 1


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
    # Migrate existing trades table
    for col, coltype in [("grade","TEXT"), ("grade_pts","INTEGER"),
                          ("gap_pct","REAL"), ("gap_dir","TEXT"),
                          ("rs","REAL"), ("entry_hour","REAL")]:
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN {} {}".format(col, coltype))
        except:
            pass

    # AI config history
    c.execute("""
        CREATE TABLE IF NOT EXISTS ai_config (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT,
            version    INTEGER,
            config_json TEXT,
            trigger    TEXT
        )
    """)

    # AI analysis log
    c.execute("""
        CREATE TABLE IF NOT EXISTS ai_analyses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT,
            trades_used INTEGER,
            win_rate    REAL,
            insight     TEXT,
            focus       TEXT,
            reasoning   TEXT,
            config_diff TEXT,
            raw_response TEXT
        )
    """)

    # AI structural proposals (new strategies / indicators / signal types)
    c.execute("""
        CREATE TABLE IF NOT EXISTS ai_proposals (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT,
            proposal_type TEXT,
            title         TEXT,
            summary       TEXT,
            evidence      TEXT,
            spec          TEXT,
            status        TEXT DEFAULT 'pending',
            dismissed_at  TEXT
        )
    """)

    # Swing trade signal cache (for history across scans)
    c.execute("""
        CREATE TABLE IF NOT EXISTS swing_signals (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts            TEXT,
            symbol        TEXT,
            signal_type   TEXT,
            direction     TEXT,
            price         REAL,
            score         REAL,
            prob_pct      INTEGER,
            fib_support   TEXT,
            t1            REAL,
            t2            REAL,
            t3            REAL,
            stop          REAL,
            option_expiry TEXT,
            option_strike REAL,
            option_prem   REAL,
            dte           INTEGER,
            notes         TEXT
        )
    """)

    conn.commit()
    conn.close()


def db_save_ai_config(config, trigger="scheduled"):
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        c.execute("""
            INSERT INTO ai_config (ts, version, config_json, trigger)
            VALUES (?, ?, ?, ?)
        """, (
            datetime.now(pytz.utc).isoformat(),
            config.get("ai_version", 0),
            json.dumps(config),
            trigger
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        log("DB save ai_config error: {}".format(e))


def db_save_ai_analysis(trades_used, win_rate, insight, focus, reasoning,
                         config_diff, raw_response):
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        c.execute("""
            INSERT INTO ai_analyses
            (ts, trades_used, win_rate, insight, focus, reasoning, config_diff, raw_response)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(pytz.utc).isoformat(),
            trades_used, win_rate, insight, focus, reasoning,
            json.dumps(config_diff), raw_response
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        log("DB save ai_analysis error: {}".format(e))


def db_load_latest_config():
    """Load most recent AI config from DB into memory on startup."""
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        c.execute("SELECT config_json FROM ai_config ORDER BY id DESC LIMIT 1")
        row = c.fetchone()
        conn.close()
        if row:
            saved = json.loads(row[0])
            update_config(saved, updated_by="db_restore")
            log("AI config v{} restored from DB".format(saved.get("ai_version", "?")))
    except Exception as e:
        log("DB load config error: {}".format(e))


def db_get_ai_analyses(limit=10):
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        c.execute("""
            SELECT ts, trades_used, win_rate, insight, focus, reasoning, config_diff
            FROM ai_analyses ORDER BY id DESC LIMIT ?
        """, (limit,))
        rows = c.fetchall()
        conn.close()
        cols = ["ts","trades_used","win_rate","insight","focus","reasoning","config_diff"]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        log("DB get ai_analyses error: {}".format(e))
        return []


def db_save_proposal(proposal_type, title, summary, evidence, spec):
    """Save a new structural proposal from the AI."""
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        # Avoid saving exact duplicate titles
        c.execute("SELECT id FROM ai_proposals WHERE title=? AND status='pending'", (title,))
        if c.fetchone():
            conn.close()
            return None
        c.execute("""
            INSERT INTO ai_proposals
            (ts, proposal_type, title, summary, evidence, spec, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
        """, (
            datetime.now(pytz.utc).isoformat(),
            proposal_type, title, summary, evidence, spec
        ))
        pid = c.lastrowid
        conn.commit()
        conn.close()
        return pid
    except Exception as e:
        log("DB save proposal error: {}".format(e))
        return None


def db_get_proposals(status=None, limit=20):
    """Get proposals, optionally filtered by status."""
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        if status:
            c.execute("""
                SELECT id, ts, proposal_type, title, summary, evidence, spec, status
                FROM ai_proposals WHERE status=?
                ORDER BY id DESC LIMIT ?
            """, (status, limit))
        else:
            c.execute("""
                SELECT id, ts, proposal_type, title, summary, evidence, spec, status
                FROM ai_proposals ORDER BY id DESC LIMIT ?
            """, (limit,))
        rows = c.fetchall()
        conn.close()
        cols = ["id","ts","proposal_type","title","summary","evidence","spec","status"]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        log("DB get proposals error: {}".format(e))
        return []


def db_dismiss_proposal(proposal_id):
    try:
        conn = sqlite3.connect(DB_FILE)
        c    = conn.cursor()
        c.execute("UPDATE ai_proposals SET status='dismissed', dismissed_at=? WHERE id=?",
                  (datetime.now(pytz.utc).isoformat(), proposal_id))
        conn.commit()
        conn.close()
    except Exception as e:
        log("DB dismiss proposal error: {}".format(e))


def db_get_all_closed_trades():
    """All closed trades for AI analysis."""
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
        cols = ["symbol","direction","outcome","pnl","r_mult",
                "grade","grade_pts","gap_pct","gap_dir","rs","entry_hour","ts"]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        log("DB get all closed trades error: {}".format(e))
        return []


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


def get_1hr_bars(symbol):
    """Last 30 x 1hr bars for key level detection."""
    try:
        r = requests.get(DATA_URL.format(symbol), headers=HEADERS,
                         params={"timeframe": "1Hour", "limit": 30}, timeout=10)
        if r.status_code != 200:
            return None
        return r.json().get("bars", [])
    except:
        return None


def get_4hr_bars(symbol):
    """Synthesize 4hr candles from 1hr bars (4 x 1hr grouped)."""
    try:
        r = requests.get(DATA_URL.format(symbol), headers=HEADERS,
                         params={"timeframe": "1Hour", "limit": 80}, timeout=10)
        if r.status_code != 200:
            return None
        bars = r.json().get("bars", [])
        if not bars:
            return None
        grouped = []
        for i in range(0, len(bars) - 3, 4):
            chunk = bars[i:i+4]
            grouped.append({
                "o": chunk[0]["o"],
                "h": max(b["h"] for b in chunk),
                "l": min(b["l"] for b in chunk),
                "c": chunk[-1]["c"],
                "v": sum(b["v"] for b in chunk),
                "t": chunk[0]["t"],
            })
        return grouped
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


# =============================================
# VWAP DEVIATION BANDS
# =============================================

def calculate_vwap_bands(bars, num_std=2.0):
    """
    Calculate VWAP and standard deviation bands for intraday bars.
    Returns: (vwap, upper_band, lower_band, std_dev)
    """
    if not bars or len(bars) < 3:
        return None, None, None, None

    cum_pv  = 0.0
    cum_vol = 0.0
    for b in bars:
        typ  = (b["h"] + b["l"] + b["c"]) / 3.0
        cum_pv  += typ * b["v"]
        cum_vol += b["v"]
    if cum_vol == 0:
        return None, None, None, None

    vwap = cum_pv / cum_vol

    cum_var = 0.0
    for b in bars:
        typ  = (b["h"] + b["l"] + b["c"]) / 3.0
        cum_var += b["v"] * (typ - vwap) ** 2
    std_dev = math.sqrt(cum_var / cum_vol) if cum_vol > 0 else 0.0

    upper = round(vwap + num_std * std_dev, 4)
    lower = round(vwap - num_std * std_dev, 4)
    return round(vwap, 4), upper, lower, round(std_dev, 4)


# =============================================
# STRATEGY: VWAP TREND FOLLOWING
# (Zarattini & Aziz, SSRN 2023 — Sharpe 2.1 on QQQ)
# =============================================

def detect_vwap_trend(intraday, daily, vwap, cfg, et_hour,
                      gap_pct, gap_dir, rs, market_bias,
                      time_vol_ratio, vol_mult):
    """
    When price stays consistently on one side of VWAP with trending
    structure (HH/HL or LH/LL), it's a high-prob continuation signal.
    Catches trending days where ORB breakout didn't cleanly trigger.

    Returns signal dict or None.
    """
    if not cfg.get("vwap_trend_enabled", True):
        return None
    if not intraday or not vwap:
        return None

    min_bars = cfg.get("vwap_trend_min_bars", 8)
    # Need enough bars past the ORB window
    if len(intraday) < min_bars + 6:
        return None

    price = intraday[-1]["c"]

    # Check consecutive bars on same side of VWAP
    recent = intraday[-min_bars:]
    above_count = sum(1 for b in recent if b["c"] > vwap)
    below_count = sum(1 for b in recent if b["c"] < vwap)

    if above_count < min_bars and below_count < min_bars:
        return None

    direction = "CALL" if above_count >= min_bars else "PUT"

    # Trending structure: higher lows (CALL) or lower highs (PUT)
    trend_bars = intraday[-min_bars:]
    if direction == "CALL":
        hl_count = sum(1 for i in range(1, len(trend_bars))
                       if trend_bars[i]["l"] >= trend_bars[i-1]["l"])
        trend_pct = hl_count / (len(trend_bars) - 1)
    else:
        lh_count = sum(1 for i in range(1, len(trend_bars))
                       if trend_bars[i]["h"] <= trend_bars[i-1]["h"])
        trend_pct = lh_count / (len(trend_bars) - 1)

    if trend_pct < cfg.get("vwap_trend_trend_pct", 0.55):
        return None

    # Distance from VWAP must be meaningful
    vwap_dist_pct = abs(price - vwap) / vwap * 100
    if vwap_dist_pct < cfg.get("vwap_trend_min_dist_pct", 0.15):
        return None

    # Volume filter
    if time_vol_ratio < cfg.get("vwap_trend_vol_min", 0.9):
        return None

    # Counter-trend filter
    if not cfg.get("counter_trend_allowed", True):
        if (direction == "CALL" and market_bias == "BEAR") or \
           (direction == "PUT" and market_bias == "BULL"):
            return None

    # Compute targets using recent range
    avg_range = statistics.mean([b["h"] - b["l"] for b in intraday[-min_bars:]])
    if direction == "CALL":
        t1   = round(price + avg_range * 1.5, 2)
        t2   = round(price + avg_range * 3.0, 2)
        stop = round(vwap - avg_range * 0.5, 2)
    else:
        t1   = round(price - avg_range * 1.5, 2)
        t2   = round(price - avg_range * 3.0, 2)
        stop = round(vwap + avg_range * 0.5, 2)

    # Score: base 50 + adjustments
    score = 50 + (trend_pct * 20) + min(vwap_dist_pct * 10, 15)
    score = score * vol_mult

    log("{}: VWAP_TREND {} | dist={:.2f}% | trend={:.0f}% | vol={:.1f}x".format(
        intraday[-1].get("t", "?")[:5] if isinstance(intraday[-1].get("t"), str) else "?",
        direction, vwap_dist_pct, trend_pct * 100, time_vol_ratio))

    return {
        "signal_type":  "VWAP_TREND",
        "direction":    direction,
        "score":        round(score, 2),
        "t1":           t1,
        "t2":           t2,
        "stop":         stop,
        "vwap_dist":    round(vwap_dist_pct, 2),
        "trend_pct":    round(trend_pct * 100, 1),
    }


# =============================================
# STRATEGY: VWAP MEAN REVERSION
# (Deviation band snap-back — works on range-bound days)
# =============================================

def detect_vwap_mean_reversion(intraday, daily, vwap, cfg, et_hour,
                                time_vol_ratio, vol_mult):
    """
    When price extends to VWAP deviation bands and shows reversal,
    trade the snap-back toward VWAP. Works best on range-bound days
    where ORB breakouts fail.

    Returns signal dict or None.
    """
    if not cfg.get("vwap_mr_enabled", True):
        return None
    min_bars = cfg.get("vwap_mr_min_bars", 12)
    if not intraday or len(intraday) < min_bars:
        return None

    num_std = cfg.get("vwap_mr_band_std", 2.0)
    vwap_val, upper, lower, std_dev = calculate_vwap_bands(intraday, num_std)
    if not vwap_val or not upper or not lower or std_dev == 0:
        return None

    price     = intraday[-1]["c"]
    prev_close = intraday[-2]["c"]

    # Volume filter
    if time_vol_ratio < cfg.get("vwap_mr_vol_min", 0.8):
        return None

    # Detect reversal from upper band (short/PUT signal)
    if prev_close >= upper and price < upper:
        direction = "PUT"
        band_tag  = "upper"
        t1   = round(vwap_val, 2)
        t2   = round(lower, 2)
        stop = round(upper + std_dev * 0.5, 2)
    # Detect reversal from lower band (long/CALL signal)
    elif prev_close <= lower and price > lower:
        direction = "CALL"
        band_tag  = "lower"
        t1   = round(vwap_val, 2)
        t2   = round(upper, 2)
        stop = round(lower - std_dev * 0.5, 2)
    else:
        return None

    # Score: distance from band matters, volume matters
    band_dist = abs(price - vwap_val) / vwap_val * 100 if vwap_val else 0
    score = 45 + min(band_dist * 8, 20) + min(time_vol_ratio * 5, 15)
    score = score * vol_mult

    log("{}: VWAP_MR {} from {} band | dist={:.2f}% | vol={:.1f}x".format(
        "?", direction, band_tag, band_dist, time_vol_ratio))

    return {
        "signal_type":  "VWAP_MEAN_REV",
        "direction":    direction,
        "score":        round(score, 2),
        "t1":           t1,
        "t2":           t2,
        "stop":         stop,
        "band_tag":     band_tag,
        "band_dist":    round(band_dist, 2),
        "upper_band":   round(upper, 2),
        "lower_band":   round(lower, 2),
    }


# =============================================
# STRATEGY: INITIAL BALANCE EXTENSION
# (Market Profile — Dalton/Steidlmayer, 65-70% continuation)
# =============================================

def detect_ib_extension(intraday, daily, vwap, cfg, et_hour,
                        time_vol_ratio, vol_mult, market_bias):
    """
    The Initial Balance is the first hour's range (bars 0-11 at 5min).
    When price moves 1x IB range beyond IB high/low, it signals strong
    directional conviction.

    Fires AFTER 10:30 AM ET — catches midday momentum that ORB misses.

    Returns signal dict or None.
    """
    if not cfg.get("ib_ext_enabled", True):
        return None
    if not intraday or len(intraday) < 14:
        return None

    # Only fire after IB period ends
    min_hour = cfg.get("ib_ext_min_hour", 10.5)
    if et_hour < min_hour:
        return None

    # Initial Balance = first 12 bars (1 hour of 5min bars)
    ib_bars = intraday[:12]
    ib_high = max(b["h"] for b in ib_bars)
    ib_low  = min(b["l"] for b in ib_bars)
    ib_range = ib_high - ib_low

    if ib_range <= 0:
        return None

    price = intraday[-1]["c"]
    multiplier = cfg.get("ib_ext_multiplier", 1.0)

    # Check for IB extension
    if price > ib_high + (ib_range * multiplier):
        direction = "CALL"
        extension = (price - ib_high) / ib_range
    elif price < ib_low - (ib_range * multiplier):
        direction = "PUT"
        extension = (ib_low - price) / ib_range
    else:
        return None

    # Volume confirmation
    if time_vol_ratio < cfg.get("ib_ext_vol_min", 1.2):
        return None

    # VWAP alignment check (price should be on the same side as direction)
    if vwap:
        if direction == "CALL" and price < vwap:
            return None  # Extension without VWAP support = likely false
        if direction == "PUT" and price > vwap:
            return None

    # Counter-trend filter
    if not cfg.get("counter_trend_allowed", True):
        if (direction == "CALL" and market_bias == "BEAR") or \
           (direction == "PUT" and market_bias == "BULL"):
            return None

    # Targets: use IB range for projection
    if direction == "CALL":
        t1   = round(price + ib_range * 0.5, 2)
        t2   = round(price + ib_range * 1.0, 2)
        stop = round(ib_high - ib_range * 0.3, 2)
    else:
        t1   = round(price - ib_range * 0.5, 2)
        t2   = round(price - ib_range * 1.0, 2)
        stop = round(ib_low + ib_range * 0.3, 2)

    # Score: extension magnitude + volume
    score = 55 + min(extension * 15, 25) + min(time_vol_ratio * 5, 15)
    score = score * vol_mult

    log("{}: IB_EXT {} | ext={:.1f}x IB | vol={:.1f}x".format(
        "?", direction, extension, time_vol_ratio))

    return {
        "signal_type":  "IB_EXTENSION",
        "direction":    direction,
        "score":        round(score, 2),
        "t1":           t1,
        "t2":           t2,
        "stop":         stop,
        "ib_high":      round(ib_high, 2),
        "ib_low":       round(ib_low, 2),
        "ib_range":     round(ib_range, 2),
        "extension":    round(extension, 2),
    }


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
    Scores 0-100 across 5 factors using AI-tunable weights from SCANNER_CONFIG.
    """
    cfg = get_config()
    pts = 0

    # 1. Breakout strength
    bs_pct = breakout_strength * 100
    w      = cfg["weight_breakout"]
    if bs_pct >= cfg["bs_strong"]:
        pts += w
    elif bs_pct >= cfg["bs_medium"]:
        pts += int(w * 0.72)
    elif bs_pct >= cfg["bs_weak"]:
        pts += int(w * 0.48)
    else:
        pts += int(w * 0.24)

    # 2. Volume ratio
    w = cfg["weight_volume"]
    if vol_ratio >= cfg["vol_high"]:
        pts += w
    elif vol_ratio >= cfg["vol_med"]:
        pts += int(w * 0.75)
    elif vol_ratio >= cfg["vol_low"]:
        pts += int(w * 0.50)
    else:
        pts += int(w * 0.20)

    # 3. Gap alignment
    w = cfg["weight_gap"]
    if direction == "CALL":
        if gap_direction == "UP" and gap_pct >= 0.5:
            pts += w
        elif gap_direction == "UP":
            pts += int(w * 0.70)
        elif gap_direction == "FLAT":
            pts += int(w * 0.40)
        else:
            pts += int(w * 0.10)
    else:
        if gap_direction == "DOWN" and abs(gap_pct) >= 0.5:
            pts += w
        elif gap_direction == "DOWN":
            pts += int(w * 0.70)
        elif gap_direction == "FLAT":
            pts += int(w * 0.40)
        else:
            pts += int(w * 0.10)

    # 4. Relative strength
    w = cfg["weight_rs"]
    if direction == "CALL":
        if rs >= 0.3:    pts += w
        elif rs >= 0.1:  pts += int(w * 0.70)
        elif rs >= -0.1: pts += int(w * 0.40)
        else:            pts += int(w * 0.10)
    else:
        if rs <= -0.3:   pts += w
        elif rs <= -0.1: pts += int(w * 0.70)
        elif rs <= 0.1:  pts += int(w * 0.40)
        else:            pts += int(w * 0.10)

    # 5. Time of day
    w = cfg["weight_time"]
    if et_hour < cfg["time_prime_end"]:
        pts += w
    elif et_hour < cfg["time_decent_end"]:
        pts += int(w * 0.67)
    elif et_hour < cfg["time_risky_end"]:
        pts += int(w * 0.33)
    else:
        pts += int(w * 0.07)

    # Apply vol regime modifier
    pts = int(pts * vol_mult)
    pts = min(pts, 100)

    a_min = cfg["grade_a_min"]
    b_min = cfg["grade_b_min"]
    c_min = cfg["grade_c_min"]

    if pts >= a_min:
        grade = "A"; color = "#3fb950"
    elif pts >= b_min:
        grade = "B"; color = "#e3b341"
    elif pts >= c_min:
        grade = "C"; color = "#f0883e"
    else:
        grade = "D"; color = "#f85149"

    return grade, pts, color


# =============================================
# MULTI-TIMEFRAME KEY LEVELS
# =============================================

def get_key_levels(daily_bars, bars_1hr, bars_4hr):
    """
    Returns sorted list of key price levels from daily/4hr/1hr charts.
    Each level: {price, label, tf, strength}  strength: 3=daily 2=4hr 1=1hr
    """
    levels = []

    if daily_bars and len(daily_bars) >= 2:
        pdh = daily_bars[-2]["h"]
        pdl = daily_bars[-2]["l"]
        levels.append({"price": pdh, "label": "PDH", "tf": "Daily", "strength": 3})
        levels.append({"price": pdl, "label": "PDL", "tf": "Daily", "strength": 3})
        week = daily_bars[-5:] if len(daily_bars) >= 5 else daily_bars
        wkh  = max(b["h"] for b in week)
        wkl  = min(b["l"] for b in week)
        if pdh and abs(wkh - pdh) / pdh > 0.002:
            levels.append({"price": wkh, "label": "WkH", "tf": "Daily", "strength": 2})
        if pdl and abs(wkl - pdl) / pdl > 0.002:
            levels.append({"price": wkl, "label": "WkL", "tf": "Daily", "strength": 2})

    if bars_4hr and len(bars_4hr) >= 6:
        for i in range(2, len(bars_4hr) - 2):
            b = bars_4hr[i]
            if (b["h"] > bars_4hr[i-1]["h"] and b["h"] > bars_4hr[i-2]["h"] and
                    b["h"] > bars_4hr[i+1]["h"] and b["h"] > bars_4hr[i+2]["h"]):
                levels.append({"price": b["h"], "label": "4H-R", "tf": "4hr", "strength": 2})
            if (b["l"] < bars_4hr[i-1]["l"] and b["l"] < bars_4hr[i-2]["l"] and
                    b["l"] < bars_4hr[i+1]["l"] and b["l"] < bars_4hr[i+2]["l"]):
                levels.append({"price": b["l"], "label": "4H-S", "tf": "4hr", "strength": 2})

    if bars_1hr and len(bars_1hr) >= 4:
        recent = bars_1hr[-10:]
        for i in range(1, len(recent) - 1):
            b = recent[i]
            if b["h"] > recent[i-1]["h"] and b["h"] > recent[i+1]["h"]:
                levels.append({"price": b["h"], "label": "1H-R", "tf": "1hr", "strength": 1})
            if b["l"] < recent[i-1]["l"] and b["l"] < recent[i+1]["l"]:
                levels.append({"price": b["l"], "label": "1H-S", "tf": "1hr", "strength": 1})

    # Sort and deduplicate within 0.1% of each other
    levels.sort(key=lambda x: x["price"])
    deduped = []
    for lvl in levels:
        if not deduped:
            deduped.append(lvl)
        elif abs(lvl["price"] - deduped[-1]["price"]) / deduped[-1]["price"] < 0.001:
            if lvl["strength"] > deduped[-1]["strength"]:
                deduped[-1] = lvl
        else:
            deduped.append(lvl)
    return deduped


def check_clear_air(price, direction, t1, t2, key_levels):
    """
    Checks if key levels block T1 or T2.
    Returns dict: clear_to_t1, clear_to_t2, blocking_level, context
    """
    if not key_levels or not t1 or not t2:
        return {"clear_to_t1": True, "clear_to_t2": True,
                "blocking_level": None, "context": "No levels identified"}

    blocking = []
    if direction == "CALL":
        for lvl in key_levels:
            if price < lvl["price"] <= t2:
                blocking.append(lvl)
        blocking.sort(key=lambda x: x["price"])
    else:
        for lvl in key_levels:
            if t2 <= lvl["price"] < price:
                blocking.append(lvl)
        blocking.sort(key=lambda x: x["price"], reverse=True)

    if not blocking:
        return {"clear_to_t1": True, "clear_to_t2": True,
                "blocking_level": None, "context": "Clear to T2"}

    nearest = blocking[0]
    if direction == "CALL":
        clear_to_t1  = nearest["price"] > t1
        pct_away     = round((nearest["price"] - price) / price * 100, 2)
    else:
        clear_to_t1  = nearest["price"] < t1
        pct_away     = round((price - nearest["price"]) / price * 100, 2)

    clear_to_t2 = all(
        (b["price"] > t1 if direction == "CALL" else b["price"] < t1)
        for b in blocking
    )

    if clear_to_t1:
        context = "Clear to T1 | {lbl} {lp:.2f} near T2".format(
            lbl=nearest["label"], lp=nearest["price"])
    else:
        context = "{lbl} {lp:.2f} blocks T1 ({pa:.2f}% away)".format(
            lbl=nearest["label"], lp=nearest["price"], pa=pct_away)

    return {
        "clear_to_t1":    clear_to_t1,
        "clear_to_t2":    clear_to_t2,
        "blocking_level": nearest,
        "context":        context,
        "all_blocking":   blocking[:3],
    }


def recommend_contract(symbol, direction, price, orb_range):
    """
    Recommends the best 0DTE contract targeting delta ~0.40 (ATM/slightly OTM).
    Returns strike, estimated premium range, and rationale.
    """
    if symbol in ("SPY", "QQQ"):
        interval = 1.0
    elif price > 500:
        interval = 5.0
    elif price > 100:
        interval = 2.5
    else:
        interval = 1.0

    # Round to nearest strike
    atm = round(round(price / interval) * interval, 2)

    if direction == "CALL":
        # First OTM call = ATM if price below ATM, else one strike above
        strike = atm if price < atm else round(atm + interval, 2)
    else:
        strike = atm if price > atm else round(atm - interval, 2)

    otm_pct = round(abs(strike - price) / price * 100, 2)
    if otm_pct < 0.1:
        desc = "ATM"
    elif otm_pct < 0.5:
        desc = "1st OTM ({:.2f}%)".format(otm_pct)
    else:
        desc = "OTM ({:.2f}%)".format(otm_pct)

    # Estimated premium: 0.35-0.65% of underlying for near-ATM 0DTE
    prem_lo = round(price * 0.0035, 2)
    prem_hi = round(price * 0.0065, 2)

    return {
        "rec_strike":    strike,
        "delta_target":  0.40,
        "prem_est_low":  prem_lo,
        "prem_est_high": prem_hi,
        "strike_desc":   desc,
        "interval":      interval,
    }


# =============================================
# 1HR TREND DETECTION
# =============================================

def get_1hr_trend(bars_1hr):
    """
    Detects 1hr chart trend by comparing the last 3 swing highs and lows.
    Returns: ('BULL'|'BEAR'|'MIXED', description_string, score -1..+1)

    BULL  = 1hr making higher highs AND higher lows  -> confirms CALL
    BEAR  = 1hr making lower highs  AND lower lows   -> confirms PUT
    MIXED = neither condition met                    -> no confirmation
    """
    if not bars_1hr or len(bars_1hr) < 8:
        return "MIXED", "Insufficient 1hr data", 0.0

    # Find swing highs and lows using a 2-bar lookback on each side
    highs, lows = [], []
    for i in range(2, len(bars_1hr) - 2):
        b = bars_1hr[i]
        if b["h"] > bars_1hr[i-1]["h"] and b["h"] > bars_1hr[i-2]["h"]                 and b["h"] > bars_1hr[i+1]["h"] and b["h"] > bars_1hr[i+2]["h"]:
            highs.append(b["h"])
        if b["l"] < bars_1hr[i-1]["l"] and b["l"] < bars_1hr[i-2]["l"]                 and b["l"] < bars_1hr[i+1]["l"] and b["l"] < bars_1hr[i+2]["l"]:
            lows.append(b["l"])

    if len(highs) < 2 or len(lows) < 2:
        return "MIXED", "Not enough 1hr pivots", 0.0

    # Use last 3 swing points
    last_highs = highs[-3:]
    last_lows  = lows[-3:]

    hh = all(last_highs[i] > last_highs[i-1] for i in range(1, len(last_highs)))
    hl = all(last_lows[i]  > last_lows[i-1]  for i in range(1, len(last_lows)))
    lh = all(last_highs[i] < last_highs[i-1] for i in range(1, len(last_highs)))
    ll = all(last_lows[i]  < last_lows[i-1]  for i in range(1, len(last_lows)))

    if hh and hl:
        return "BULL", "1hr HH+HL confirmed", +1.0
    elif lh and ll:
        return "BEAR", "1hr LH+LL confirmed", -1.0
    elif hh or hl:
        return "BULL", "1hr partial uptrend", +0.5
    elif lh or ll:
        return "BEAR", "1hr partial downtrend", -0.5
    else:
        return "MIXED", "1hr ranging/choppy", 0.0


# =============================================
# TIME-ADJUSTED VOLUME
# =============================================

def get_time_vol_ratio(intraday_today, daily_bars, current_bar_idx):
    """
    Compares current bar's volume to the historical average volume
    at the same time-of-day slot over the last N trading days.

    Uses the intraday 5min bar index as a time proxy:
      bar 0 = 9:30, bar 1 = 9:35, ..., bar 6 = 10:00, etc.

    Returns: (ratio: float, label: str)
      ratio > 2.0  = exceptional volume
      ratio > 1.5  = elevated
      ratio > 1.0  = normal
      ratio < 0.8  = light volume
    """
    if not intraday_today or not daily_bars:
        return 1.0, "N/A"

    current_bar_idx = max(0, min(current_bar_idx, len(intraday_today) - 1))
    current_vol     = intraday_today[current_bar_idx]["v"]

    # We need historical intraday data per day - we don't fetch that separately,
    # so we approximate using the full-day volume and scaling by time-of-day.
    # Better approximation: use the last 10 daily bars to get average daily volume,
    # then use the fraction of day elapsed to estimate expected cumulative volume.
    if len(daily_bars) < 5:
        return 1.0, "N/A"

    avg_daily_vol = statistics.mean([b["v"] for b in daily_bars[-10:]])
    if avg_daily_vol <= 0:
        return 1.0, "N/A"

    # Fraction of trading day elapsed (78 bars = full day of 5min bars)
    day_fraction  = max(0.01, (current_bar_idx + 1) / 78.0)

    # Volume typically front-loaded: first 30min ~25% of daily, so weight early bars
    # Simple model: expected vol at bar N = avg_daily * (bar_fraction ^ 0.7)
    expected_vol  = avg_daily_vol * (day_fraction ** 0.7)

    # Scale expected to per-bar
    expected_per_bar = expected_vol / (current_bar_idx + 1) if current_bar_idx > 0 else expected_vol

    ratio = round(current_vol / expected_per_bar, 2) if expected_per_bar > 0 else 1.0

    if ratio >= 3.0:
        label = "Exceptional ({:.1f}x)".format(ratio)
    elif ratio >= 2.0:
        label = "High ({:.1f}x)".format(ratio)
    elif ratio >= 1.3:
        label = "Elevated ({:.1f}x)".format(ratio)
    elif ratio >= 0.8:
        label = "Normal ({:.1f}x)".format(ratio)
    else:
        label = "Light ({:.1f}x)".format(ratio)

    return ratio, label


# =============================================
# COMPOSITE RANK SCORE
# =============================================

def compute_rank_score(result):
    """
    Single number 0-200 for ranking signals. All bonuses/penalties
    driven by SCANNER_CONFIG so the AI can tune them.
    """
    cfg        = get_config()
    grade_pts  = result.get("grade_pts", 0)
    aligned    = result.get("aligned", True)
    direction  = result.get("direction", "")
    late       = result.get("late_entry", False)

    score = grade_pts

    # Alignment
    score += cfg["rank_align_bonus"] if aligned else cfg["rank_align_penalty"]

    # 1hr trend
    trend     = result.get("trend_1hr", "MIXED")
    trend_scr = result.get("trend_score", 0.0)
    if direction == "CALL":
        if trend == "BULL":
            score += cfg["rank_trend_full"] if trend_scr >= 1.0 else cfg["rank_trend_partial"]
        elif trend == "BEAR":
            score += cfg["rank_trend_oppose"]
    elif direction == "PUT":
        if trend == "BEAR":
            score += cfg["rank_trend_full"] if trend_scr <= -1.0 else cfg["rank_trend_partial"]
        elif trend == "BULL":
            score += cfg["rank_trend_oppose"]

    # Time-adjusted volume
    tv_ratio = result.get("time_vol_ratio", 1.0)
    if tv_ratio >= 3.0:
        score += cfg["rank_vol_exceptional"]
    elif tv_ratio >= 2.0:
        score += cfg["rank_vol_elevated"]
    elif tv_ratio < 0.8:
        score += cfg["rank_vol_light"]

    # Clear air
    ca = result.get("clear_air") or {}
    if ca.get("clear_to_t2"):
        score += cfg["rank_clear_t2"]
    elif ca.get("clear_to_t1"):
        score += cfg["rank_clear_t1"]
    else:
        score += cfg["rank_blocked"]

    # Late penalty
    if late:
        score += cfg["rank_late_penalty"]

    return max(0, score)


# =============================================
# OPTIONS
# =============================================

def _next_expiry_dates(n=4):
    """
    Returns today + next N expiry dates (skipping weekends).
    Individual stocks only have weekly (Fri) options.
    ETFs like SPY/QQQ have daily options.
    """
    import datetime as _dt
    et   = pytz.timezone("America/New_York")
    base = datetime.now(et).date()
    dates = []
    d = base
    while len(dates) < n + 1:
        dates.append(d.strftime("%Y-%m-%d"))
        d = d + _dt.timedelta(days=1)
        while d.weekday() >= 5:
            d = d + _dt.timedelta(days=1)
    return dates


def get_liquid_option(symbol, direction, underlying_price=None):
    """
    Fetch the nearest available ATM option via Alpaca options snapshot API.

    SPY/QQQ have daily 0DTE options.
    Individual stocks (TSLA, AMD, etc.) only have weekly options (Fri expiry).
    We try today first, then fall back up to 5 trading days out.

    Returns (premium, strike, is_live, dte_label)
      dte_label: '0DTE', '1DTE', '2DTE' etc. shown on card
    """
    import datetime as _dt

    if not ALPACA_KEY or not ALPACA_SECRET:
        log("Alpaca keys not set - cannot fetch options")
        return None, None, False, None

    option_type = "call" if direction == "CALL" else "put"
    et          = pytz.timezone("America/New_York")
    today_date  = datetime.now(et).date()

    if underlying_price:
        lo = round(underlying_price * 0.98, 2)
        hi = round(underlying_price * 1.02, 2)
    else:
        lo, hi = None, None

    headers = {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    url = "https://data.alpaca.markets/v1beta1/options/snapshots/{}".format(symbol)

    expiry_dates = _next_expiry_dates(n=5)

    for expiry_str in expiry_dates:
        params = {
            "feed":            "indicative",
            "expiration_date": expiry_str,
            "type":            option_type,
            "limit":           50,
        }
        if lo is not None:
            params["strike_price_gte"] = lo
            params["strike_price_lte"] = hi

        try:
            r = requests.get(url, headers=headers, params=params, timeout=10)
            log("Alpaca options {} {} {}: HTTP {}".format(
                symbol, option_type, expiry_str, r.status_code))

            if r.status_code == 422:
                log("  No options for {} on {} -- trying next expiry".format(symbol, expiry_str))
                continue
            if r.status_code != 200:
                log("  Options error {}: {}".format(r.status_code, r.text[:150]))
                return None, None, False, None

            snapshots = r.json().get("snapshots", {})
            log("  {} contracts for {} {}".format(len(snapshots), symbol, expiry_str))

            candidates = []
            for contract_sym, snap in snapshots.items():
                try:
                    strike = int(contract_sym[-8:]) / 1000.0
                except Exception:
                    continue

                quote = snap.get("latestQuote") or {}
                bid   = float(quote.get("bp") or 0)
                ask   = float(quote.get("ap") or 0)

                if bid > 0 and ask > 0:
                    mid = round((bid + ask) / 2, 2)
                elif ask > 0:
                    mid = round(ask, 2)
                else:
                    continue

                if not (0.05 <= mid <= 50.00):
                    continue

                greeks = snap.get("greeks") or {}
                delta  = abs(float(greeks.get("delta") or 0))
                iv     = float(snap.get("impliedVolatility") or 0)

                candidates.append({
                    "strike": strike,
                    "price":  mid,
                    "bid":    bid,
                    "ask":    ask,
                    "delta":  delta,
                    "iv":     iv,
                })

            if not candidates:
                log("  No liquid ATM candidates on {} -- trying next".format(expiry_str))
                continue

            if any(c["delta"] > 0 for c in candidates):
                candidates.sort(key=lambda x: abs(x["delta"] - 0.40))
            elif underlying_price:
                candidates.sort(key=lambda x: abs(x["strike"] - underlying_price))

            best = candidates[0]

            exp_date  = _dt.datetime.strptime(expiry_str, "%Y-%m-%d").date()
            dte       = (exp_date - today_date).days
            dte_label = "{}DTE".format(dte) if dte > 0 else "0DTE"

            log("  Selected {} {}: strike={} delta={:.3f} mid={} ({})".format(
                symbol, option_type, best["strike"], best["delta"],
                best["price"], dte_label))

            return best["price"], best["strike"], True, dte_label

        except Exception as e:
            log("Alpaca options exception {} {}: {}".format(symbol, expiry_str, e))
            return None, None, False, None

    log("  No options found for {} across all expiries".format(symbol))
    return None, None, False, None


# =============================================
# SWING ENGINE -- DATA FETCHERS
# =============================================

def get_daily_extended(symbol, limit=90):
    try:
        r = requests.get(DATA_URL.format(symbol), headers=HEADERS,
                         params={"timeframe": "1Day", "limit": limit}, timeout=10)
        if r.status_code != 200:
            return None
        return r.json().get("bars", [])
    except:
        return None


def get_weekly_bars(symbol, limit=52):
    try:
        r = requests.get(DATA_URL.format(symbol), headers=HEADERS,
                         params={"timeframe": "1Week", "limit": limit}, timeout=10)
        if r.status_code != 200:
            return None
        return r.json().get("bars", [])
    except:
        return None


# =============================================
# SWING ENGINE -- FIBONACCI
# =============================================

FIBO_RETRACE = [0.236, 0.382, 0.500, 0.618, 0.786]
FIBO_EXTEND  = [1.000, 1.272, 1.618, 2.000, 2.618]


def fibonacci_levels(swing_low, swing_high, direction="CALL"):
    """
    Retracements = pullback support (CALL) or bounce resistance (PUT).
    Extensions   = upside targets (CALL) or downside targets (PUT).
    """
    rng = swing_high - swing_low
    if rng <= 0:
        return {}, {}
    retrace = {}
    extend  = {}
    if direction == "CALL":
        for r in FIBO_RETRACE:
            retrace[r] = round(swing_high - rng * r, 2)
        for e in FIBO_EXTEND:
            extend[e]  = round(swing_low  + rng * e, 2)
    else:
        for r in FIBO_RETRACE:
            retrace[r] = round(swing_low  + rng * r, 2)
        for e in FIBO_EXTEND:
            extend[e]  = round(swing_high - rng * e, 2)
    return retrace, extend


def nearest_fib(price, levels):
    if not levels:
        return None, None, None
    best_r = min(levels, key=lambda r: abs(levels[r] - price))
    best_v = levels[best_r]
    pct    = round(abs(price - best_v) / price * 100, 2)
    return best_v, best_r, pct


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
    cfg = get_config()  # snapshot config for this entire scan

    # Broad market alignment
    spy_chg_global = get_spy_change()
    qqq_intra = get_intraday("QQQ")
    qqq_chg = 0.0
    if qqq_intra and len(qqq_intra) >= 2 and qqq_intra[0]["o"]:
        qqq_chg = round((qqq_intra[-1]["c"] - qqq_intra[0]["o"]) / qqq_intra[0]["o"] * 100, 3)
    if spy_chg_global > 0.2 and qqq_chg > 0.2:
        market_bias = "BULL"
    elif spy_chg_global < -0.2 and qqq_chg < -0.2:
        market_bias = "BEAR"
    else:
        market_bias = "MIXED"
    log("Market bias: {} | SPY {:.2f}% QQQ {:.2f}%".format(market_bias, spy_chg_global, qqq_chg))

    for symbol in SYMBOLS:
        result = {
            "symbol": symbol, "direction": None, "score": 0,
            "grade": None, "grade_pts": 0, "grade_color": "#8b949e",
            "price": None, "premium": None, "strike": None,
            "contracts": None, "stop": None, "target": None,
            "status": "scanning", "vwap": None,
            "orb_high": None, "orb_low": None,
            "vs_orb": None, "vs_vwap": None, "vol_ratio": None,
            "gap_pct": None, "gap_dir": None, "rs": None,
            "spy_chg": spy_chg_global, "late_entry": False,
            "market_bias": market_bias, "aligned": True,
            "key_levels": [], "clear_air": None, "rec_contract": None,
            "t1_prob": 50, "t2_prob": 25, "signal_type": "ORB",
        }

        intraday = get_intraday(symbol)
        daily    = get_daily(symbol)
        bars_1hr = get_1hr_bars(symbol)
        bars_4hr = get_4hr_bars(symbol)

        if not intraday or len(intraday) < ORB_BARS + 2 or not daily:
            result["status"] = "no data"; results.append(result); continue

        vol_mult = volatility_score(daily)
        if vol_mult == 0.0:
            result["status"] = "dead market"; results.append(result); continue

        orb      = intraday[:ORB_BARS]
        orb_high = max(b["h"] for b in orb)
        orb_low  = min(b["l"] for b in orb)
        current  = intraday[-1]
        price    = current["c"]
        vwap     = calculate_vwap(intraday)

        if not vwap:
            result["status"] = "no vwap"; results.append(result); continue

        orb_range   = orb_high - orb_low
        vs_orb_high = round((price - orb_high) / orb_high * 100, 3)
        vs_orb_low  = round((orb_low  - price) / orb_low  * 100, 3)
        vs_vwap     = round((price    - vwap)  / vwap     * 100, 3)

        gap_pct, gap_dir = get_premarket_gap(daily, intraday)
        sym_chg          = get_symbol_change(intraday)
        rs               = relative_strength(sym_chg, spy_chg_global)

        et         = pytz.timezone("America/New_York")
        et_now     = datetime.now(et)
        et_hour    = et_now.hour + et_now.minute / 60.0
        late_entry = et_hour >= 14.0

        key_levels  = get_key_levels(daily, bars_1hr, bars_4hr)

        # 1hr trend confirmation
        trend_1hr, trend_desc, trend_score = get_1hr_trend(bars_1hr)

        # Time-adjusted volume (index of current bar in today's session)
        bar_idx                = len(intraday) - 1
        time_vol_ratio, tv_lbl = get_time_vol_ratio(intraday, daily, bar_idx)

        result["price"]      = round(price, 2)
        result["vwap"]       = round(vwap, 2)
        result["orb_high"]   = round(orb_high, 2)
        result["orb_low"]    = round(orb_low, 2)
        result["vol_mult"]   = round(vol_mult, 2)
        result["gap_pct"]    = gap_pct
        result["gap_dir"]    = gap_dir
        result["rs"]         = rs
        result["late_entry"]     = late_entry
        result["key_levels"]     = key_levels
        result["trend_1hr"]      = trend_1hr
        result["trend_desc"]     = trend_desc
        result["trend_score"]    = trend_score
        result["time_vol_ratio"] = time_vol_ratio
        result["time_vol_lbl"]   = tv_lbl

        if orb_range > 0:
            result["und_call_t1"]   = round(price + orb_range, 2)
            result["und_call_t2"]   = round(price + orb_range * 2, 2)
            result["und_call_stop"] = round(price - orb_range * 0.5, 2)
            result["und_put_t1"]    = round(price - orb_range, 2)
            result["und_put_t2"]    = round(price - orb_range * 2, 2)
            result["und_put_stop"]  = round(price + orb_range * 0.5, 2)
            avg_range = statistics.mean([b["h"] - b["l"] for b in daily[-10:]])
            if avg_range > 0:
                result["t1_prob"] = round(max(20, min(85, 100 - (orb_range / avg_range * 100))), 0)
                result["t2_prob"] = round(max(10, min(55, 100 - (orb_range * 2 / avg_range * 100))), 0)

        direction = None
        breakout_strength = 0

        if price > orb_high and price > vwap:
            direction         = "CALL"
            breakout_strength = (price - orb_high) / orb_high
            result["vs_orb"]  = "+{:.3f}%".format(abs(vs_orb_high))
            result["vs_vwap"] = "+{:.3f}%".format(abs(vs_vwap))
        elif price < orb_low and price < vwap:
            direction         = "PUT"
            breakout_strength = (orb_low - price) / orb_low
            result["vs_orb"]  = "-{:.3f}%".format(abs(vs_orb_low))
            result["vs_vwap"] = "-{:.3f}%".format(abs(vs_vwap))
        else:
            # --- No ORB breakout: try alternative strategies ---
            alt_signal = None

            # 1. VWAP Trend Following (Zarattini & Aziz)
            if not alt_signal:
                alt_signal = detect_vwap_trend(
                    intraday, daily, vwap, cfg, et_hour,
                    gap_pct, gap_dir, rs, market_bias,
                    time_vol_ratio, vol_mult)

            # 2. VWAP Mean Reversion (deviation band snap-back)
            if not alt_signal:
                alt_signal = detect_vwap_mean_reversion(
                    intraday, daily, vwap, cfg, et_hour,
                    time_vol_ratio, vol_mult)

            # 3. Initial Balance Extension (Market Profile)
            if not alt_signal:
                alt_signal = detect_ib_extension(
                    intraday, daily, vwap, cfg, et_hour,
                    time_vol_ratio, vol_mult, market_bias)

            if alt_signal:
                # We have an alternative signal — promote to full signal
                direction = alt_signal["direction"]
                result["signal_type"] = alt_signal["signal_type"]
                result["direction"]   = direction
                result["vs_vwap"]     = "{:+.3f}%".format(vs_vwap)
                result["vs_orb"]      = "{:.2f}% from ORB {}".format(
                    abs(vs_orb_high if price > vwap else vs_orb_low),
                    "high" if price > vwap else "low")

                # Use alt signal's targets
                if direction == "CALL":
                    result["und_call_t1"]   = alt_signal.get("t1", result.get("und_call_t1"))
                    result["und_call_t2"]   = alt_signal.get("t2", result.get("und_call_t2"))
                    result["und_call_stop"] = alt_signal.get("stop", result.get("und_call_stop"))
                else:
                    result["und_put_t1"]    = alt_signal.get("t1", result.get("und_put_t1"))
                    result["und_put_t2"]    = alt_signal.get("t2", result.get("und_put_t2"))
                    result["und_put_stop"]  = alt_signal.get("stop", result.get("und_put_stop"))

                # Grade the alt signal (use moderate breakout strength proxy)
                vol_ratio = current["v"] / intraday[-2]["v"] if intraday[-2]["v"] > 0 else 1
                breakout_strength = abs(price - vwap) / vwap if vwap else 0
                score = alt_signal["score"]

                result["aligned"] = not (
                    (direction == "CALL" and market_bias == "BEAR") or
                    (direction == "PUT"  and market_bias == "BULL")
                )

                grade, grade_pts, grade_color = confluence_grade(
                    breakout_strength, vol_ratio, vol_mult,
                    gap_pct, gap_dir, rs, direction, et_hour)

                # Alt strategies get a slight grade bump for passing their own filters
                grade_pts = min(grade_pts + 5, 100)

                t1_key = "und_call_t1" if direction == "CALL" else "und_put_t1"
                t2_key = "und_call_t2" if direction == "CALL" else "und_put_t2"
                clear_air = check_clear_air(price, direction,
                                            result.get(t1_key), result.get(t2_key),
                                            key_levels)
                result["clear_air"] = clear_air

                if not clear_air["clear_to_t1"]:
                    grade_pts = min(grade_pts, 52)
                    if grade in ("A", "B"):
                        grade = "C"; grade_color = "#f0883e"

                result["rec_contract"] = recommend_contract(symbol, direction, price, orb_range)

                if grade == "D":
                    result["status"]    = "low grade"
                    result["score"]     = round(score, 2)
                    result["grade"]     = grade
                    result["grade_pts"] = grade_pts
                    results.append(result)
                    log("{}: ALT {} SKIP D grade {}pts".format(
                        symbol, alt_signal["signal_type"], grade_pts))
                    continue

                premium, strike, is_live, dte_label = get_liquid_option(symbol, direction, price)

                if premium and is_live:
                    contracts, stp, tgt = calculate_contracts(premium, score)
                    result["premium"]   = round(premium, 2)
                    result["strike"]    = strike
                    result["contracts"] = contracts
                    result["stop"]      = stp
                    result["target"]    = tgt
                    result["dte_label"] = dte_label or "0DTE"
                    result["status"]    = "SIGNAL"
                else:
                    result["status"] = "SIGNAL (no options)"

                result["score"]       = round(score, 2)
                result["grade"]       = grade
                result["grade_pts"]   = grade_pts
                result["grade_color"] = grade_color
                results.append(result)
                log("{} {} | {} {} {}pts | alt_strategy".format(
                    symbol, direction, alt_signal["signal_type"], grade, grade_pts))
                continue

            # No alternative signal either — fall through to WATCHING
            result["direction"] = "CALL" if price > vwap else "PUT"
            result["vs_vwap"]   = "{:+.3f}%".format(vs_vwap)
            result["vs_orb"]    = "{:.2f}% from ORB {}".format(
                abs(vs_orb_high if price > vwap else vs_orb_low),
                "high" if price > vwap else "low")
            vol_ratio        = current["v"] / intraday[-2]["v"] if intraday[-2]["v"] > 0 else 1
            result["score"]  = round((1 - min(abs(vs_orb_high), abs(vs_orb_low)) / 100) * vol_mult * 10, 2)
            result["status"] = "WATCHING"
            results.append(result)
            continue

        vol_ratio = current["v"] / intraday[-2]["v"] if intraday[-2]["v"] > 0 else 1
        score     = (breakout_strength * 100 + vol_ratio) * vol_mult

        result["aligned"] = not (
            (direction == "CALL" and market_bias == "BEAR") or
            (direction == "PUT"  and market_bias == "BULL")
        )

        # If counter-trend not allowed by config, skip signal
        if not result["aligned"] and not cfg["counter_trend_allowed"]:
            result["status"] = "counter-trend filtered"
            results.append(result)
            log("{}: SKIP counter-trend (config disabled)".format(symbol))
            continue

        grade, grade_pts, grade_color = confluence_grade(
            breakout_strength, vol_ratio, vol_mult,
            gap_pct, gap_dir, rs, direction, et_hour)

        t1_key = "und_call_t1" if direction == "CALL" else "und_put_t1"
        t2_key = "und_call_t2" if direction == "CALL" else "und_put_t2"
        clear_air = check_clear_air(price, direction,
                                    result.get(t1_key), result.get(t2_key),
                                    key_levels)
        result["clear_air"] = clear_air

        if not clear_air["clear_to_t1"]:
            grade_pts = min(grade_pts, 52)
            if grade in ("A", "B"):
                grade = "C"; grade_color = "#f0883e"
                log("  {} grade capped C: {}".format(symbol, clear_air["context"]))
        elif clear_air["clear_to_t2"] and grade_pts >= 70:
            grade_pts = min(grade_pts + 5, 100)

        result["rec_contract"] = recommend_contract(symbol, direction, price, orb_range)

        if grade == "D":
            result["status"]    = "low grade"
            result["direction"] = direction
            result["score"]     = round(score, 2)
            result["grade"]     = grade
            results.append(result)
            log("{}: SKIP D grade {}pts".format(symbol, grade_pts))
            continue

        # -------------------------------------------------------
        # VWAP RECLAIM STRATEGY (AI-enabled secondary signal)
        # Detects: price reclaimed VWAP after being below/above it
        # -------------------------------------------------------
        vwap_reclaim_signal = False
        if cfg.get("vwap_reclaim_enabled") and len(intraday) >= cfg.get("vwap_reclaim_lookback", 6) + 1:
            lookback = int(cfg.get("vwap_reclaim_lookback", 6))
            prev_bars = intraday[-(lookback+1):-1]
            cur_close = intraday[-1]["c"]
            cur_open  = intraday[-1]["o"]

            if direction == "CALL":
                # At least half of lookback bars were below VWAP, now above
                below_count = sum(1 for b in prev_bars if b["c"] < vwap)
                just_reclaimed = cur_close > vwap and intraday[-2]["c"] < vwap
                if just_reclaimed and below_count >= lookback // 2:
                    if time_vol_ratio >= cfg.get("vwap_reclaim_vol_min", 1.3):
                        vwap_reclaim_signal = True
                        log("{}: VWAP RECLAIM detected (CALL) vol={:.1f}x".format(
                            symbol, time_vol_ratio))
            else:
                above_count = sum(1 for b in prev_bars if b["c"] > vwap)
                just_lost = cur_close < vwap and intraday[-2]["c"] > vwap
                if just_lost and above_count >= lookback // 2:
                    if time_vol_ratio >= cfg.get("vwap_reclaim_vol_min", 1.3):
                        vwap_reclaim_signal = True
                        log("{}: VWAP RECLAIM detected (PUT) vol={:.1f}x".format(
                            symbol, time_vol_ratio))

            if vwap_reclaim_signal:
                result["signal_type"] = "VWAP_RECLAIM"
                # Bump grade one level for clean VWAP reclaim with volume
                if grade == "C":
                    grade = "B"; grade_color = "#e3b341"; grade_pts = min(grade_pts + 10, 74)
                result["grade_pts"] += 5  # small bonus in rank

        premium, strike, is_live, dte_label = get_liquid_option(symbol, direction, price)

        if premium and is_live:
            contracts, stp, tgt = calculate_contracts(premium, score)
            result["premium"]   = round(premium, 2)
            result["strike"]    = strike
            result["contracts"] = contracts
            result["stop"]      = stp
            result["target"]    = tgt
            result["dte_label"] = dte_label or "0DTE"
            result["status"]    = "SIGNAL"
        else:
            result["status"] = "SIGNAL (no options)"

        result["direction"]   = direction
        result["score"]       = round(score, 2)
        result["grade"]       = grade
        result["grade_pts"]   = grade_pts
        result["grade_color"] = grade_color
        results.append(result)
        log("{} {} | {} {}pts | aligned={} | clear_t1={} | {}".format(
            symbol, direction, grade, grade_pts,
            result["aligned"], clear_air["clear_to_t1"], clear_air["context"]))

    # Compute composite rank score for every confirmed signal
    for r in results:
        if r.get("status") in ("SIGNAL", "SIGNAL (no options)"):
            r["rank_score"] = compute_rank_score(r)
        else:
            r["rank_score"] = 0

    # Tag the top-ranked signal as PRIMARY
    active = [r for r in results if r.get("rank_score", 0) > 0]
    if active:
        best = max(active, key=lambda r: r["rank_score"])
        best["is_primary"] = True

    def sort_key(r):
        s  = r.get("status", "")
        rs = -r.get("rank_score", 0)
        if s == "SIGNAL":              return (0, rs)
        if s == "SIGNAL (no options)": return (1, rs)
        if s == "WATCHING":            return (2, -r.get("score", 0))
        return (4, 0)

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
                "{sig_type} SIGNAL\n\n"
                "Symbol: {}\nDirection: {}\nScore: {}\n\n"
                "Underlying: ${}\nStrike: {}\nPremium: ${}\n\n"
                "Contracts: {}\nStop: ${}\nTarget: ${}\n\n"
                "Vol Multiplier: {}x"
            ).format(
                sig["symbol"], sig["direction"], sig["score"],
                sig["price"], sig["strike"], sig["premium"],
                sig["contracts"], sig["stop"], sig["target"],
                sig.get("vol_mult", 1.0),
                sig_type=sig.get("signal_type", "ORB")
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
# AI IMPROVEMENT ENGINE
# =============================================

_ai_last_run_date  = ""   # tracks last calendar day AI ran
_ai_last_trade_cnt = 0    # tracks trade count at last AI run

def _build_stats_summary(trades):
    """Build a compact stats dict for the AI prompt."""
    if not trades:
        return {}

    total  = len(trades)
    wins   = [t for t in trades if t["outcome"] == "WIN"]
    losses = [t for t in trades if t["outcome"] == "LOSS"]
    wr     = round(len(wins) / total * 100, 1) if total else 0
    avg_r  = round(sum(t["r_mult"] or 0 for t in trades) / total, 2) if total else 0
    total_pnl = round(sum(t["pnl"] or 0 for t in trades), 2)

    def breakdown(key):
        groups = {}
        for t in trades:
            k = str(t.get(key) or "?")
            if k not in groups: groups[k] = {"w": 0, "l": 0, "pnl": 0}
            if t["outcome"] == "WIN":  groups[k]["w"] += 1
            else:                      groups[k]["l"] += 1
            groups[k]["pnl"] = round(groups[k]["pnl"] + (t["pnl"] or 0), 2)
        result = {}
        for k, v in groups.items():
            n = v["w"] + v["l"]
            result[k] = {
                "trades": n,
                "win_rate": round(v["w"] / n * 100, 1) if n else 0,
                "pnl": v["pnl"]
            }
        return result

    # Time buckets
    def time_bucket(h):
        if h < 10:   return "9:30-10:00"
        elif h < 11: return "10:00-11:00"
        elif h < 12: return "11:00-12:00"
        elif h < 13: return "12:00-1:00"
        elif h < 14: return "1:00-2:00"
        else:        return "2:00+"

    time_groups = {}
    for t in trades:
        k = time_bucket(t["entry_hour"] or 9.5)
        if k not in time_groups: time_groups[k] = {"w": 0, "l": 0, "pnl": 0}
        if t["outcome"] == "WIN":  time_groups[k]["w"] += 1
        else:                      time_groups[k]["l"] += 1
        time_groups[k]["pnl"] = round(time_groups[k]["pnl"] + (t["pnl"] or 0), 2)
    by_time = {}
    for k, v in time_groups.items():
        n = v["w"] + v["l"]
        by_time[k] = {"trades": n, "win_rate": round(v["w"]/n*100,1) if n else 0, "pnl": v["pnl"]}

    # RS sign breakdown
    rs_pos = [t for t in trades if (t["rs"] or 0) > 0]
    rs_neg = [t for t in trades if (t["rs"] or 0) <= 0]
    rs_breakdown = {
        "rs_positive": {
            "trades": len(rs_pos),
            "win_rate": round(sum(1 for t in rs_pos if t["outcome"]=="WIN") / len(rs_pos) * 100, 1) if rs_pos else 0
        },
        "rs_negative": {
            "trades": len(rs_neg),
            "win_rate": round(sum(1 for t in rs_neg if t["outcome"]=="WIN") / len(rs_neg) * 100, 1) if rs_neg else 0
        }
    }

    return {
        "total_trades": total,
        "win_rate":     wr,
        "avg_r_mult":   avg_r,
        "total_pnl":    total_pnl,
        "by_symbol":    breakdown("symbol"),
        "by_grade":     breakdown("grade"),
        "by_direction": breakdown("direction"),
        "by_gap_dir":   breakdown("gap_dir"),
        "by_time":      by_time,
        "rs_breakdown": rs_breakdown,
        "recent_10":    trades[:10]
    }


def run_ai_improvement(trigger="scheduled"):
    """
    Calls Claude API with trade history + current config.
    Gets back updated parameters and insight.
    Applies changes immediately to live scanner.
    """
    global _ai_last_run_date, _ai_last_trade_cnt

    if not ANTHROPIC_KEY:
        log("AI: ANTHROPIC_API_KEY not set - skipping improvement run")
        return

    trades = db_get_all_closed_trades()
    if len(trades) < 5:
        log("AI: Only {} closed trades - need at least 5 to analyze".format(len(trades)))
        return

    log("AI: Starting improvement run ({} trades, trigger={})".format(len(trades), trigger))

    stats   = _build_stats_summary(trades)
    cfg     = get_config()

    # Remove non-tunable keys from what we send
    cfg_tunable = {k: v for k, v in cfg.items()
                   if k not in ("ai_insight", "ai_focus", "updated_at", "updated_by")}

    prompt = """You are an expert quantitative trader and algorithm optimizer.
You are analyzing a 0DTE (zero days to expiration) options day trading scanner.
Your ONLY goal is to maximize the scanner's win rate and present the highest-conviction trade each day.

## Current Scanner Config
```json
{config}
```

## Trade Statistics
```json
{stats}
```

## Your Task
Analyze the trade data and return an updated configuration that will improve win rate.
Focus on:
1. Which grade thresholds should shift based on actual win rates by grade?
2. Which factor weights should increase/decrease based on which factors correlate with wins?
3. Should counter-trend signals be filtered out entirely?
4. Is the late_entry_hour cutoff optimal?
5. Should VWAP reclaim strategy be enabled given the data?
6. What is the single highest-conviction setup pattern in this data?

## Rules
- Only tune parameters that have statistical support (10+ trades in a group before drawing conclusions)
- Be conservative -- never change a weight by more than 5 points in one update
- Grade thresholds: A min must stay >= 65, C min must stay >= 25
- The config changes must make mathematical sense (weights should roughly sum to 100)
- If data is insufficient for a dimension, leave that parameter unchanged

## Response Format
Respond ONLY with valid JSON, no markdown, no explanation outside the JSON:
{{
  "config_updates": {{
    "weight_breakout": <int>,
    "weight_volume": <int>,
    "weight_gap": <int>,
    "weight_rs": <int>,
    "weight_time": <int>,
    "grade_a_min": <int>,
    "grade_b_min": <int>,
    "grade_c_min": <int>,
    "late_entry_hour": <float>,
    "counter_trend_allowed": <bool>,
    "vwap_reclaim_enabled": <bool>,
    "rank_align_bonus": <int>,
    "rank_align_penalty": <int>,
    "rank_late_penalty": <int>
  }},
  "insight": "<one sentence: the single most important pattern found>",
  "focus": "<one sentence: what the scanner should prioritize tomorrow>",
  "reasoning": "<3-5 sentences explaining the key config changes and why>"
}}""".format(
        config=json.dumps(cfg_tunable, indent=2),
        stats=json.dumps(stats, indent=2)
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "messages":   [{"role": "user", "content": prompt}]
            },
            timeout=30
        )
        log("AI: Anthropic HTTP {}".format(resp.status_code))

        if resp.status_code != 200:
            log("AI: API error: {}".format(resp.text[:200]))
            return

        raw = resp.json()["content"][0]["text"].strip()
        log("AI: Got response ({} chars)".format(len(raw)))

        # Strip any accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        parsed = json.loads(raw)

        updates      = parsed.get("config_updates", {})
        insight      = parsed.get("insight", "")
        focus        = parsed.get("focus", "")
        reasoning    = parsed.get("reasoning", "")

        # Compute diff for logging
        old_cfg    = get_config()
        config_diff = {k: {"old": old_cfg.get(k), "new": v}
                       for k, v in updates.items()
                       if old_cfg.get(k) != v}

        # Apply to live config
        updates["ai_insight"] = insight
        updates["ai_focus"]   = focus
        update_config(updates, updated_by="ai_v{}".format(
            old_cfg.get("ai_version", 0) + 1))

        new_cfg = get_config()
        db_save_ai_config(new_cfg, trigger=trigger)
        db_save_ai_analysis(
            trades_used  = len(trades),
            win_rate     = stats["win_rate"],
            insight      = insight,
            focus        = focus,
            reasoning    = reasoning,
            config_diff  = config_diff,
            raw_response = raw
        )

        et = pytz.timezone("America/New_York")
        _ai_last_run_date  = datetime.now(et).strftime("%Y-%m-%d")
        _ai_last_trade_cnt = len(trades)

        log("AI: Config updated to v{} | {}".format(new_cfg["ai_version"], insight))
        if config_diff:
            for k, v in config_diff.items():
                log("AI:   {} {} -> {}".format(k, v["old"], v["new"]))

        # Notify via Telegram
        msg = (
            "AI CONFIG UPDATE v{}\n\n"
            "Insight: {}\n\n"
            "Focus: {}\n\n"
            "Changes: {}"
        ).format(
            new_cfg["ai_version"],
            insight,
            focus,
            ", ".join("{}: {}->{}".format(k, v["old"], v["new"])
                      for k, v in config_diff.items()) or "none"
        )
        send_telegram(msg)

        # --- Phase 2: Structural proposals ---
        _run_proposal_analysis(trades, stats)

    except json.JSONDecodeError as e:
        log("AI: JSON parse error: {} | raw: {}".format(e, raw[:200]))
    except Exception as e:
        log("AI: Exception: {}".format(e))

def _run_proposal_analysis(trades, stats):
    """
    Second AI pass: looks for structural improvements the config system
    cannot handle -- new strategies, indicators, signal types.
    Saves proposals to DB and sends top one via Telegram.
    """
    if not ANTHROPIC_KEY or len(trades) < 10:
        return

    log("AI: Running structural proposal analysis...")

    # Pull any pending proposals so we don't repeat them
    existing = db_get_proposals(status="pending", limit=10)
    existing_titles = [p["title"] for p in existing]

    cfg = get_config()

    prompt = """You are an expert quant analyzing a 0DTE options day trading scanner.
Your job is to identify structural improvements that CANNOT be done by tuning config numbers.
These are new strategies, new signal types, or new indicator calculations that require new code.

## Current Scanner Capabilities
- ORB (Opening Range Breakout): detects price breaking above ORB high or below ORB low with VWAP confirmation
- Confluence grading: breakout strength, volume, gap alignment, relative strength vs SPY, time of day
- 1hr trend confirmation: HH/HL or LH/LL pattern on 1hr bars
- VWAP reclaim: price reclaims VWAP after being below/above it (currently enabled/disabled via config)
- Key levels: previous day high/low, weekly high/low, 4hr and 1hr swing points
- Clear air check: detects key levels blocking path to targets

## Trade Statistics
```json
{stats}
```

## Already Pending Proposals (do not repeat these)
{existing}

## Your Task
Based on the trade data, identify up to 2 structural proposals. Each proposal must:
1. Be justified by a clear pattern in the data (or a notable gap where data is thin)
2. Be something that requires new code -- not just a config number change
3. Be specific enough for a developer to implement in one session

Proposal types to consider:
- NEW_STRATEGY: entirely new signal type (e.g. gap fill, VWAP band touch, opening drive)
- NEW_INDICATOR: new calculation added to existing signals (e.g. pre-market volume, sector RS, options flow)
- NEW_FILTER: new condition to filter out low quality signals (e.g. earnings filter, news filter)
- NEW_SIZING: new position sizing logic (e.g. scale size by grade AND vol regime together)
- DASHBOARD: new display or alert that helps decision making

Respond ONLY with valid JSON, no markdown:
{{
  "proposals": [
    {{
      "type": "NEW_STRATEGY|NEW_INDICATOR|NEW_FILTER|NEW_SIZING|DASHBOARD",
      "title": "<short title, max 60 chars>",
      "summary": "<1-2 sentences: what it is and why it helps>",
      "evidence": "<what in the trade data specifically suggests this would help>",
      "spec": "<detailed implementation spec: trigger conditions, parameters, how it integrates with existing grading system, expected impact on win rate>"
    }}
  ]
}}

If the data does not yet support any specific proposal, return: {{"proposals": []}}""".format(
        stats=json.dumps(stats, indent=2),
        existing=", ".join(existing_titles) if existing_titles else "none"
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-20250514",
                "max_tokens": 1500,
                "messages":   [{"role": "user", "content": prompt}]
            },
            timeout=30
        )

        if resp.status_code != 200:
            log("AI proposals: API error {}".format(resp.status_code))
            return

        raw = resp.json()["content"][0]["text"].strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        raw = raw.strip()

        parsed   = json.loads(raw)
        proposals = parsed.get("proposals", [])
        log("AI proposals: {} proposals generated".format(len(proposals)))

        saved = []
        for p in proposals:
            pid = db_save_proposal(
                proposal_type = p.get("type", "NEW_STRATEGY"),
                title         = p.get("title", ""),
                summary       = p.get("summary", ""),
                evidence      = p.get("evidence", ""),
                spec          = p.get("spec", "")
            )
            if pid:
                saved.append(p)
                log("AI proposal saved: [{}] {}".format(p.get("type"), p.get("title")))

        # Telegram: send the top new proposal
        if saved:
            top = saved[0]
            tg_msg = (
                "AI STRUCTURAL PROPOSAL\n\n"
                "[{ptype}] {title}\n\n"
                "{summary}\n\n"
                "Evidence: {evidence}\n\n"
                "View full spec at /ai"
            ).format(
                ptype   = top.get("type",""),
                title   = top.get("title",""),
                summary = top.get("summary",""),
                evidence= top.get("evidence","")[:200]
            )
            send_telegram(tg_msg)

    except json.JSONDecodeError as e:
        log("AI proposals: JSON parse error: {}".format(e))
    except Exception as e:
        log("AI proposals: Exception: {}".format(e))


# =============================================
# SWING TRADE SCANNER
# =============================================

def get_daily_bars(symbol, limit=60):
    """Fetch daily OHLCV bars from Alpaca."""
    try:
        r = requests.get(
            DATA_URL.format(symbol),
            headers=HEADERS,
            params={"timeframe": "1Day", "limit": limit, "adjustment": "split"},
            timeout=10
        )
        if r.status_code != 200:
            return []
        return r.json().get("bars", [])
    except Exception as e:
        log("daily_bars {}: {}".format(symbol, e))
        return []


def swing_fibonacci(bars, direction="CALL"):
    """
    Find the dominant swing low and swing high in last 20 daily bars.
    Returns dict with retracement supports and extension targets.
    direction: CALL = bullish (extensions above price), PUT = bearish
    """
    if len(bars) < 10:
        return None

    recent = bars[-20:]
    highs  = [b["h"] for b in recent]
    lows   = [b["l"] for b in recent]

    swing_high = max(highs)
    swing_low  = min(lows)
    rng        = swing_high - swing_low

    if rng <= 0:
        return None

    if direction == "CALL":
        # Bullish: measure from swing low up
        base   = swing_low
        top    = swing_high
        retrace_levels = {
            "23.6": round(top - rng * 0.236, 2),
            "38.2": round(top - rng * 0.382, 2),
            "50.0": round(top - rng * 0.500, 2),
            "61.8": round(top - rng * 0.618, 2),
            "78.6": round(top - rng * 0.786, 2),
        }
        ext_levels = {
            "127.2": round(base + rng * 1.272, 2),
            "161.8": round(base + rng * 1.618, 2),
            "200.0": round(base + rng * 2.000, 2),
            "261.8": round(base + rng * 2.618, 2),
        }
    else:
        # Bearish: measure from swing high down
        base   = swing_high
        top    = swing_low
        retrace_levels = {
            "23.6": round(top + rng * 0.236, 2),
            "38.2": round(top + rng * 0.382, 2),
            "50.0": round(top + rng * 0.500, 2),
            "61.8": round(top + rng * 0.618, 2),
            "78.6": round(top + rng * 0.786, 2),
        }
        ext_levels = {
            "127.2": round(base - rng * 1.272, 2),
            "161.8": round(base - rng * 1.618, 2),
            "200.0": round(base - rng * 2.000, 2),
            "261.8": round(base - rng * 2.618, 2),
        }

    return {
        "swing_low":  swing_low,
        "swing_high": swing_high,
        "range":      round(rng, 2),
        "retracements": retrace_levels,
        "extensions":   ext_levels,
    }


# =============================================
# SWING ENGINE -- UTILITY
# =============================================

def _avg_vol(bars, lookback=50):
    """Average daily volume over lookback bars, ignoring zeros."""
    vols = [b["v"] for b in bars[-lookback:] if b.get("v", 0) > 0]
    return statistics.mean(vols) if vols else 1


def _sma(bars, n, field="c"):
    """Simple moving average of last n bars."""
    vals = [b[field] for b in bars[-n:]]
    return statistics.mean(vals) if len(vals) == n else None


def _atr(bars, n=14):
    """Average True Range over n bars."""
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i-1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0
    return statistics.mean(trs[-n:]) if len(trs) >= n else statistics.mean(trs)


def _high52w(bars):
    return max(b["h"] for b in bars) if bars else 0

def _low52w(bars):
    return min(b["l"] for b in bars) if bars else 0


# =============================================
# SWING ENGINE -- WEINSTEIN STAGE FILTER
# =============================================

def weinstein_stage(bars, weekly_bars=None):
    """
    Stan Weinstein Stage Analysis.

    Stage 1: Basing -- price flat around 30-week SMA, low volume
    Stage 2: Markup  -- price above rising 30-week SMA, expanding volume  <-- BUY ZONE
    Stage 3: Top     -- price extended, SMA flattening
    Stage 4: Decline -- price below falling 30-week SMA                   <-- AVOID / SHORT

    Uses 30-week SMA on weekly bars, or proxies with 150-day SMA on daily.
    Returns: 1, 2, 3, or 4 (int) and slope direction.
    """
    src = weekly_bars if (weekly_bars and len(weekly_bars) >= 35) else None

    if src:
        # True Weinstein: 30-week SMA
        sma30w_now  = _sma(src, 30) if len(src) >= 30 else None
        sma30w_prev = statistics.mean([b["c"] for b in src[-35:-5]]) if len(src) >= 35 else None
        price       = src[-1]["c"]
        avg_vol_w   = _avg_vol(src, 30)
        recent_vol  = statistics.mean([b["v"] for b in src[-4:]]) if len(src) >= 4 else avg_vol_w
        vol_expand  = recent_vol > avg_vol_w * 1.1
    else:
        # Proxy: 150-day SMA on daily bars (~= 30 weeks)
        if not bars or len(bars) < 160:
            return 2, True   # default: assume Stage 2 if insufficient data
        sma30w_now  = _sma(bars, 150)
        sma30w_prev = statistics.mean([b["c"] for b in bars[-160:-10]]) if len(bars) >= 160 else None
        price       = bars[-1]["c"]
        avg_vol_w   = _avg_vol(bars, 50)
        recent_vol  = statistics.mean([b["v"] for b in bars[-10:]])
        vol_expand  = recent_vol > avg_vol_w * 1.1

    if sma30w_now is None or sma30w_prev is None:
        return 2, True

    sma_rising  = sma30w_now > sma30w_prev
    sma_falling = sma30w_now < sma30w_prev

    # Stage boundaries
    pct_above = (price - sma30w_now) / sma30w_now * 100

    if price > sma30w_now and sma_rising:
        if pct_above > 30:
            return 3, True   # extended / topping
        return 2, True       # sweet spot
    elif price > sma30w_now and not sma_rising:
        return 3, False      # above but SMA rolling over
    elif price < sma30w_now and sma_falling:
        return 4, False      # decline
    else:
        return 1, False      # basing / below flat SMA


# =============================================
# SWING ENGINE -- DETECTOR 1: O'NEIL PIVOT BREAKOUT
# =============================================

def detect_oneil_pivot(daily, weekly=None):
    """
    William O'Neil / IBD Cup-with-Handle & Pivot Breakout.

    Criteria (all must be met):
    1. Stock within 10% of 52-week high (must be in a leadership position)
    2. Formed a consolidation base: 4-15 weeks, ATR contracted >= 25% vs prior ATR
    3. Breakout above the base high (pivot point) on volume >= 40% above 50-day avg
    4. Close near the high of the breakout day (> midpoint of day's range)
    5. Weinstein Stage 2 (stock in markup phase, not topping or declining)

    Probability calibration from O'Neil research:
    - All 5 criteria met: ~65% hit T1 in trending market
    - Volume >= 2x average on breakout: bump to ~70%
    - RS vs market positive: bump to ~72%

    Returns dict or None.
    """
    if not daily or len(daily) < 60:
        return None

    price      = daily[-1]["c"]
    high_52w   = _high52w(daily[-252:] if len(daily) >= 252 else daily)
    avg_vol_50 = _avg_vol(daily, 50)

    # 1. Within 10% of 52-week high (leadership requirement)
    if price < high_52w * 0.90:
        return None

    # 2. Find consolidation base: last 4-15 weeks (~20-75 trading days)
    #    Base = a range where price traded within 15% of each other
    #    Look for the most recent base
    base_end   = len(daily) - 1
    base_start = None

    for lookback in range(20, 76):
        segment = daily[-(lookback):-1]
        if len(segment) < 10:
            continue
        seg_high = max(b["h"] for b in segment)
        seg_low  = min(b["l"] for b in segment)
        if seg_low > 0 and (seg_high - seg_low) / seg_low <= 0.18:
            base_start = len(daily) - lookback
            base_high  = seg_high
            base_low   = seg_low
            break

    if base_start is None:
        return None

    # ATR contraction: base ATR should be < 75% of prior-period ATR
    base_bars  = daily[base_start:-1]
    prior_bars = daily[max(0, base_start - len(base_bars)):base_start]
    if len(base_bars) < 5 or len(prior_bars) < 5:
        return None

    atr_base  = _atr(base_bars,  min(14, len(base_bars)))
    atr_prior = _atr(prior_bars, min(14, len(prior_bars)))
    if atr_prior > 0 and atr_base / atr_prior > 0.85:
        return None   # ATR not contracted enough -- not a tight base

    # 3. Breakout above base high (pivot) -- check today or last 1-2 days
    today = daily[-1]
    pivot_broken = False
    days_since_breakout = 0

    for lookback_days in range(0, 4):
        bar = daily[-(1 + lookback_days)]
        vol_ratio = bar["v"] / avg_vol_50 if avg_vol_50 > 0 else 0
        if bar["h"] > base_high and bar["c"] > base_high * 0.995:
            if vol_ratio >= 1.40:   # 40%+ above average (O'Neil requirement)
                pivot_broken       = True
                days_since_breakout = lookback_days
                breakout_vol_ratio  = vol_ratio
                break

    if not pivot_broken:
        return None

    # 4. Breakout day close near high (conviction)
    brk_bar    = daily[-(1 + days_since_breakout)]
    day_range  = brk_bar["h"] - brk_bar["l"]
    close_rank = (brk_bar["c"] - brk_bar["l"]) / day_range if day_range > 0 else 0.5
    if close_rank < 0.50:   # closed in lower half of range = weak
        return None

    # 5. Weinstein Stage 2 filter
    stage, sma_rising = weinstein_stage(daily, weekly)
    if stage not in (2,):
        return None   # only take Stage 2 breakouts

    # Probability calculation (O'Neil-calibrated)
    prob = 62
    if breakout_vol_ratio >= 2.0:   prob += 8    # strong institutional conviction
    elif breakout_vol_ratio >= 1.6: prob += 4
    if days_since_breakout == 0:    prob += 5    # fresh breakout today
    if close_rank >= 0.80:          prob += 4    # closed at top of range
    if atr_base / (atr_prior + 0.001) < 0.60:   prob += 3   # very tight base
    prob = min(prob, 82)

    # Swing low = base low (stop placement)
    swing_low  = base_low
    swing_high = base_high

    retrace, extend = fibonacci_levels(swing_low, swing_high, "CALL")
    nfv, nfr, nfd   = nearest_fib(price, retrace)

    base_weeks = round((len(daily) - 1 - base_start) / 5, 1)

    return {
        "signal_type":   "ONEIL_PIVOT",
        "signal_label":  "O'Neil Pivot Breakout",
        "direction":     "CALL",
        "prob":          prob,
        "swing_low":     swing_low,
        "swing_high":    swing_high,
        "retrace":       retrace,
        "extend":        extend,
        "near_fib_val":  nfv,
        "near_fib_r":    nfr,
        "fib_dist":      nfd,
        "vol_vs_avg":    round(breakout_vol_ratio, 2),
        "days_since":    days_since_breakout,
        "earn_gap":      round((price - base_high) / base_high * 100, 2),
        "base_weeks":    base_weeks,
        "pivot":         round(base_high, 2),
        "close_rank":    round(close_rank * 100, 1),
        "stage":         stage,
        "notes":         "Base: {:.0f}wks | Pivot: ${:.2f} | Vol: {:.1f}x | Close rank: {:.0f}%".format(
                            base_weeks, base_high, breakout_vol_ratio, close_rank * 100),
    }


# =============================================
# SWING ENGINE -- DETECTOR 2: WYCKOFF SPRING
# =============================================

def detect_wyckoff_spring(daily, weekly=None):
    """
    Wyckoff Accumulation Spring -- highest-probability Wyckoff entry.

    The Spring is an engineered shakeout:
    1. Support level has been tested >= 3 times (establishes the line)
    2. Price breaks briefly BELOW support on LOW volume (the spring/trap)
    3. Price immediately recovers back above support within 1-3 days
    4. Recovery day has above-average volume (institutional buying the dip)
    5. This sets up a "Sign of Strength" rally

    Historical hit rate from Wyckoff methodology: ~68-72% when all criteria met.

    Also detects Wyckoff Distribution (opposite: breaks above resistance then fails)
    for PUT signals.

    Returns dict or None.
    """
    if not daily or len(daily) < 40:
        return None

    avg_vol = _avg_vol(daily, 50)
    price   = daily[-1]["c"]

    # Find a well-tested support level (3+ touches in last 60 bars)
    # Support = a price level where lows clustered within 1.5%
    candidate_supports = []
    lows = [b["l"] for b in daily[-60:]]

    for i, base_low in enumerate(lows):
        touches = sum(1 for l in lows if abs(l - base_low) / base_low < 0.015)
        if touches >= 3:
            candidate_supports.append(base_low)

    # Deduplicate: cluster nearby supports
    support_levels = []
    for lvl in candidate_supports:
        if not any(abs(lvl - existing) / existing < 0.02 for existing in support_levels):
            support_levels.append(lvl)

    if not support_levels:
        return None

    # Take the most-tested support in the last 60 bars
    support = max(support_levels, key=lambda lvl: sum(
        1 for l in lows if abs(l - lvl) / lvl < 0.015))

    # Look for a spring: price briefly dipped below support then recovered
    # Check last 1-5 bars for the spring
    spring_bar  = None
    spring_idx  = None
    for lookback in range(1, 6):
        bar  = daily[-(1 + lookback)]
        prev = daily[-(2 + lookback)] if lookback + 2 <= len(daily) else None
        if prev is None:
            continue

        # Spring criteria:
        # a) Low went below support
        # b) Close recovered above support
        # c) Volume was LOW on the spring day (no panic -- it's engineered)
        vol_ratio = bar["v"] / avg_vol if avg_vol > 0 else 1
        dipped_below  = bar["l"] < support * 0.998
        closed_above  = bar["c"] > support * 0.995
        low_volume    = vol_ratio < 1.0   # should be below average (not panic selling)

        if dipped_below and closed_above and low_volume:
            spring_bar  = bar
            spring_idx  = len(daily) - 1 - lookback
            spring_vol  = vol_ratio
            spring_low  = bar["l"]
            break

    if spring_bar is None:
        return None

    # Confirm recovery: current price must be above support
    if price < support * 0.998:
        return None

    # Check recovery volume is higher (Sign of Strength)
    recent_bars    = daily[spring_idx + 1:]
    if recent_bars:
        recovery_vol   = statistics.mean([b["v"] for b in recent_bars])
        vol_expanding  = recovery_vol > avg_vol * 0.9
    else:
        vol_expanding  = False

    # Weinstein Stage: spring should occur in Stage 1/2 (accumulation zone)
    stage, _ = weinstein_stage(daily, weekly)
    if stage == 4:
        return None   # don't buy a spring in a confirmed downtrend

    # Count support touches for probability calibration
    support_touches = sum(1 for l in lows if abs(l - support) / support < 0.015)

    # Probability (Wyckoff-calibrated)
    prob = 64
    if support_touches >= 4:     prob += 6
    if support_touches >= 5:     prob += 4
    if vol_expanding:            prob += 6
    if spring_vol < 0.7:         prob += 4   # very low spring vol = cleaner trap
    if stage in (1, 2):          prob += 4
    prob = min(prob, 80)

    # Fib levels: use last major swing for extension targets
    swing_low  = spring_low
    swing_high = max(b["h"] for b in daily[-60:])

    retrace, extend = fibonacci_levels(swing_low, swing_high, "CALL")
    nfv, nfr, nfd   = nearest_fib(price, retrace)

    days_since = len(daily) - 1 - spring_idx

    return {
        "signal_type":   "WYCKOFF_SPRING",
        "signal_label":  "Wyckoff Spring",
        "direction":     "CALL",
        "prob":          prob,
        "swing_low":     swing_low,
        "swing_high":    swing_high,
        "retrace":       retrace,
        "extend":        extend,
        "near_fib_val":  nfv,
        "near_fib_r":    nfr,
        "fib_dist":      nfd,
        "vol_vs_avg":    round(spring_vol, 2),
        "days_since":    days_since,
        "earn_gap":      round((price - support) / support * 100, 2),
        "support_level": round(support, 2),
        "touches":       support_touches,
        "stage":         stage,
        "notes":         "Support: ${:.2f} ({} touches) | Spring vol: {:.1f}x | {} days ago".format(
                            support, support_touches, spring_vol, days_since),
    }


# =============================================
# SWING ENGINE -- DETECTOR 3: 52-WEEK HIGH BREAKOUT
# =============================================

def detect_52w_breakout(daily, weekly=None):
    """
    52-Week High Breakout with Tight Consolidation.

    Academic research (Moskowitz, Jegadeesh & Titman momentum papers) and
    O'Neil's data both confirm: stocks making new 52-week highs on volume
    while in a tight consolidation (3+ weeks within 10% of the high) have
    the highest forward 3-month return of any signal type.

    Criteria:
    1. Price within 3% of all-time-high in the data (or 52-week high)
    2. Spent >= 3 weeks within 10% of that high (showing distribution has ended)
    3. Volume contraction during consolidation (supply dried up)
    4. Breakout: new high on volume >= 30% above 50-day avg
    5. Stage 2 filter (must be in markup phase)

    Probability: ~60-65% (slightly lower than O'Neil because less specific entry)
    """
    if not daily or len(daily) < 60:
        return None

    price      = daily[-1]["c"]
    high_all   = _high52w(daily)
    avg_vol_50 = _avg_vol(daily, 50)

    # 1. Must be within 5% of all-time high in dataset
    if price < high_all * 0.95:
        return None

    # 2. Tight consolidation: >= 15 bars (3 weeks) within 10% of high
    tight_bars = [b for b in daily[-50:] if b["c"] >= high_all * 0.90]
    if len(tight_bars) < 15:
        return None

    # 3. Volume contraction during consolidation
    consol_vol   = statistics.mean([b["v"] for b in daily[-20:]])
    prior_vol    = _avg_vol(daily[-50:-20], 30) if len(daily) >= 50 else avg_vol_50
    vol_contraction = consol_vol < prior_vol * 0.90

    # 4. Breakout bar: new closing high on above-average volume
    today     = daily[-1]
    prev_high = max(b["h"] for b in daily[-50:-1]) if len(daily) >= 2 else 0
    new_high  = today["c"] >= prev_high * 0.998   # within 0.2% of prior high counts
    brk_vol   = today["v"] / avg_vol_50 if avg_vol_50 > 0 else 1

    # Also accept if breakout was in last 3 bars
    recent_breakout = False
    for lb in range(0, 4):
        bar     = daily[-(1 + lb)]
        bar_vol = bar["v"] / avg_vol_50 if avg_vol_50 > 0 else 1
        if bar["c"] >= prev_high * 0.998 and bar_vol >= 1.30:
            recent_breakout  = True
            brk_vol          = bar_vol
            days_since_break = lb
            break

    if not recent_breakout:
        return None

    # 5. Stage 2 filter
    stage, sma_rising = weinstein_stage(daily, weekly)
    if stage not in (2,):
        return None

    # Probability
    prob = 58
    if vol_contraction:          prob += 5
    if brk_vol >= 1.6:           prob += 5
    if brk_vol >= 2.0:           prob += 4
    if len(tight_bars) >= 25:    prob += 4   # longer base = more reliable
    if days_since_break == 0:    prob += 4   # fresh today
    prob = min(prob, 78)

    # Consolidation range for fib
    consol_low  = min(b["l"] for b in daily[-20:])
    consol_high = max(b["h"] for b in daily[-20:])

    retrace, extend = fibonacci_levels(consol_low, consol_high, "CALL")
    nfv, nfr, nfd   = nearest_fib(price, retrace)

    return {
        "signal_type":   "HI52_BREAKOUT",
        "signal_label":  "52-Week High Breakout",
        "direction":     "CALL",
        "prob":          prob,
        "swing_low":     consol_low,
        "swing_high":    consol_high,
        "retrace":       retrace,
        "extend":        extend,
        "near_fib_val":  nfv,
        "near_fib_r":    nfr,
        "fib_dist":      nfd,
        "vol_vs_avg":    round(brk_vol, 2),
        "days_since":    days_since_break,
        "earn_gap":      round((price / prev_high - 1) * 100, 2),
        "stage":         stage,
        "notes":         "52w high: ${:.2f} | Break vol: {:.1f}x | {} tight weeks".format(
                            high_all, brk_vol, round(len(tight_bars) / 5, 1)),
    }


# =============================================
# SWING ENGINE -- DETECTOR 4: EARNINGS CONTINUATION
#   (same pattern, now gated by Stage 2 filter)
# =============================================

def detect_earnings_continuation(daily, weekly=None):
    """
    Post-earnings gap continuation. Proven edge: stocks that gap up on earnings
    with strong volume and hold the gap continue higher 60-65% of the time
    within the next 3 weeks (O'Neil, Zacks research).

    Now gated by Stage 2 filter -- earnings pops in Stage 3/4 are traps.
    """
    if not daily or len(daily) < 25:
        return None

    avg_vol = _avg_vol(daily, 30)
    price   = daily[-1]["c"]

    # Find earnings gap: >3% gap on > 2x volume in last 20 bars
    earn_idx = None
    for i in range(len(daily) - 20, len(daily) - 2):
        b, prev = daily[i], daily[i - 1]
        if b["v"] / avg_vol >= 2.0 and abs((b["o"] - prev["c"]) / prev["c"] * 100) >= 3.0:
            earn_idx  = i
            earn_gap  = (b["o"] - prev["c"]) / prev["c"] * 100
            earn_open = b["o"]
            pre_close = prev["c"]
            break

    if earn_idx is None:
        return None

    days_since = len(daily) - 1 - earn_idx
    if not (1 <= days_since <= 20):
        return None

    direction = "CALL" if earn_gap > 0 else "PUT"

    # Gap must be mostly held (< 40% given back)
    gap_size = abs(earn_open - pre_close) or 0.01
    if direction == "CALL":
        pullback = max(0, (earn_open - price) / gap_size)
    else:
        pullback = max(0, (price - earn_open) / gap_size)
    if pullback > 0.40:
        return None

    # Stage 2 filter (only take CALL continuations in Stage 2)
    stage, _ = weinstein_stage(daily, None)
    if direction == "CALL" and stage not in (2,):
        return None

    # Probability (O'Neil / Zacks calibrated)
    prob  = 60
    prob += max(0, 12 - days_since)
    prob += 8 if pullback < 0.15 else 0
    prob += 5 if earn_gap > 6.0  else 0
    prob  = min(prob, 82)

    pre_earn   = daily[max(0, earn_idx - 10):earn_idx]
    swing_low  = min(b["l"] for b in pre_earn) if pre_earn else daily[earn_idx]["l"]
    swing_high = max(b["h"] for b in (pre_earn + [daily[earn_idx]])) if pre_earn else daily[earn_idx]["h"]

    retrace, extend = fibonacci_levels(swing_low, swing_high, direction)
    nfv, nfr, nfd   = nearest_fib(price, retrace)

    vol_vs_avg = round(statistics.mean([b["v"] for b in daily[-5:]]) / avg_vol, 2)

    return {
        "signal_type":   "EARNINGS_CONT",
        "signal_label":  "Post-Earnings Continuation",
        "direction":     direction,
        "prob":          prob,
        "swing_low":     swing_low,
        "swing_high":    swing_high,
        "retrace":       retrace,
        "extend":        extend,
        "near_fib_val":  nfv,
        "near_fib_r":    nfr,
        "fib_dist":      nfd,
        "vol_vs_avg":    vol_vs_avg,
        "days_since":    days_since,
        "earn_gap":      round(earn_gap, 2),
        "stage":         stage,
        "notes":         "Gap: {:+.1f}% | {} days ago | Pullback: {:.0f}% | Stage {}".format(
                            earn_gap, days_since, pullback * 100, stage),
    }


# =============================================
# SWING ENGINE -- PER-SYMBOL ORCHESTRATOR
# =============================================

def scan_swing_symbol(symbol):
    """
    Tier 1: fully-triggered signals (O'Neil, Wyckoff Spring, 52W, Earnings Cont.)
    Tier 2: pattern-forming watchlist (base building, accum zone, near high, gap holding)
    """
    try:
        daily  = get_daily_extended(symbol, limit=260)
        weekly = get_weekly_bars(symbol, limit=52)
        if not daily or len(daily) < 60:
            return None
        current = daily[-1]["c"]
        if not current or current <= 0:
            return None

        spy_d  = get_daily_extended("SPY", limit=10)
        spy_rs = 0.0
        if spy_d and len(spy_d) >= 5:
            spy_chg = (spy_d[-1]["c"] - spy_d[-5]["c"]) / spy_d[-5]["c"] * 100
            sym_chg = (daily[-1]["c"]  - daily[-5]["c"]) / daily[-5]["c"] * 100
            spy_rs  = round(sym_chg - spy_chg, 2)

        type_pri = {"ONEIL_PIVOT": 4, "WYCKOFF_SPRING": 3, "HI52_BREAKOUT": 2, "EARNINGS_CONT": 1}

        tier1_sigs = []
        for fn in [lambda: detect_oneil_pivot(daily, weekly),
                   lambda: detect_wyckoff_spring(daily, weekly),
                   lambda: detect_52w_breakout(daily, weekly),
                   lambda: detect_earnings_continuation(daily, weekly)]:
            try:
                r = fn()
                if r:
                    r["tier"] = 1
                    tier1_sigs.append(r)
            except Exception:
                pass

        tier2_sigs = []
        for fn in [lambda: watch_oneil_base_forming(daily, weekly),
                   lambda: watch_wyckoff_accumulation(daily, weekly),
                   lambda: watch_52w_approaching(daily, weekly),
                   lambda: watch_earnings_gap_holding(daily, weekly)]:
            try:
                r = fn()
                if r:
                    r["tier"] = 2
                    tier2_sigs.append(r)
            except Exception:
                pass

        all_sigs = tier1_sigs + tier2_sigs
        if not all_sigs:
            return None

        if tier1_sigs:
            best = max(tier1_sigs, key=lambda s: (s["prob"], type_pri.get(s["signal_type"], 0)))
        else:
            best = max(tier2_sigs, key=lambda s: s["prob"])

        direction = best["direction"]
        tier      = best["tier"]
        opt = get_swing_option(symbol, direction, current, days_out=14) if tier == 1 else None

        exts = best.get("extend", {})
        if direction == "CALL":
            above = sorted([(r, v) for r, v in exts.items() if v > current], key=lambda x: x[1])
        else:
            above = sorted([(r, v) for r, v in exts.items() if v < current],
                           key=lambda x: x[1], reverse=True)

        t1 = above[0][1] if len(above) >= 1 else None
        t2 = above[1][1] if len(above) >= 2 else None
        t3 = above[2][1] if len(above) >= 3 else None

        stop = (best.get("retrace", {}).get(0.618)
                or round(current * (0.96 if direction == "CALL" else 1.04), 2))
        rr1  = round(abs(t1 - current) / abs(current - stop), 2) if (
            t1 and stop and abs(current - stop) > 0) else 0

        return {
            "symbol":         symbol,
            "price":          round(current, 2),
            "direction":      direction,
            "tier":           tier,
            "signal_type":    best["signal_type"],
            "signal_label":   best["signal_label"],
            "prob":           best["prob"],
            "spy_rs":         spy_rs,
            "t1":             t1,
            "t2":             t2,
            "t3":             t3,
            "stop":           round(stop, 2),
            "rr1":            rr1,
            "swing_low":      best.get("swing_low"),
            "swing_high":     best.get("swing_high"),
            "retrace":        best.get("retrace", {}),
            "extend":         best.get("extend",  {}),
            "near_fib_val":   best.get("near_fib_val"),
            "near_fib_r":     best.get("near_fib_r"),
            "fib_dist":       best.get("fib_dist"),
            "vol_vs_avg":     best.get("vol_vs_avg"),
            "earn_gap":       best.get("earn_gap"),
            "days_since":     best.get("days_since"),
            "pct_from_pivot": best.get("pct_from_pivot"),
            "notes":          best.get("notes", ""),
            "stage":          best.get("stage"),
            "all_types":      [s["signal_type"] for s in all_sigs],
            "option":         opt,
        }
    except Exception as e:
        log("Swing {} error: {}".format(symbol, e))
        return None


def watch_wyckoff_accumulation(daily, weekly=None):
    if not daily or len(daily) < 40:
        return None
    avg_vol = _avg_vol(daily, 50)
    price   = daily[-1]["c"]
    recent  = daily[-40:]
    lows    = [b["l"] for b in recent]
    support_levels = []
    for base_low in lows:
        touches = sum(1 for l in lows if abs(l - base_low) / (base_low or 1) < 0.018)
        if touches >= 3:
            if not any(abs(base_low - e) / (e or 1) < 0.02 for e in support_levels):
                support_levels.append(base_low)
    if not support_levels:
        return None
    support = max(support_levels, key=lambda lvl: sum(
        1 for l in lows if abs(l - lvl) / (lvl or 1) < 0.018))
    touches    = sum(1 for l in lows if abs(l - support) / (support or 1) < 0.018)
    pct_above  = (price - support) / (support or 1) * 100
    if not (0 <= pct_above <= 8):
        return None
    recent_vol = statistics.mean([b["v"] for b in daily[-10:]])
    prior_vol  = statistics.mean([b["v"] for b in daily[-30:-10]]) if len(daily) >= 30 else avg_vol
    vol_quiet  = recent_vol < prior_vol * 0.85
    stage, _   = weinstein_stage(daily, weekly)
    if stage == 4:
        return None
    prob  = 44
    prob += 6 if touches >= 4 else 0
    prob += 4 if touches >= 5 else 0
    prob += 5 if vol_quiet else 0
    prob += 4 if stage in (1, 2) else 0
    prob  = min(prob, 60)
    swing_high = max(b["h"] for b in recent)
    retrace, extend = fibonacci_levels(support, swing_high, "CALL")
    nfv, nfr, nfd   = nearest_fib(price, retrace)
    return {"signal_type": "WATCH_WYCKOFF", "signal_label": "Wyckoff Accum. Zone",
            "direction": "CALL", "tier": 2, "prob": prob,
            "swing_low": support, "swing_high": swing_high,
            "retrace": retrace, "extend": extend,
            "near_fib_val": nfv, "near_fib_r": nfr, "fib_dist": nfd,
            "vol_vs_avg": round(recent_vol / avg_vol, 2),
            "days_since": 0, "earn_gap": 0, "stage": stage,
            "pct_from_pivot": round(pct_above, 2),
            "notes": "Support ${:.2f} ({} touches) | {:.1f}% above | {}".format(
                support, touches, pct_above, "Vol quiet" if vol_quiet else "Watch vol")}


def watch_52w_approaching(daily, weekly=None):
    if not daily or len(daily) < 40:
        return None
    price    = daily[-1]["c"]
    high_52w = _high52w(daily[-252:] if len(daily) >= 252 else daily)
    avg_vol  = _avg_vol(daily, 50)
    pct_from_high = (high_52w - price) / (high_52w or 1) * 100
    if not (1.0 <= pct_from_high <= 12.0):
        return None
    tight_bars = [b for b in daily[-30:] if b["c"] >= high_52w * 0.88]
    if len(tight_bars) < 10:
        return None
    consol_vol = statistics.mean([b["v"] for b in daily[-15:]])
    vol_contracting = consol_vol < avg_vol * 0.88
    stage, sma_rising = weinstein_stage(daily, weekly)
    if stage in (3, 4):
        return None
    prob  = 40
    prob += 6 if vol_contracting else 0
    prob += 6 if pct_from_high < 4.0 else 0
    prob += 5 if stage == 2 else 0
    prob += 4 if len(tight_bars) >= 20 else 0
    prob  = min(prob, 58)
    consol_low = min(b["l"] for b in daily[-20:])
    retrace, extend = fibonacci_levels(consol_low, high_52w, "CALL")
    nfv, nfr, nfd   = nearest_fib(price, retrace)
    return {"signal_type": "WATCH_52W", "signal_label": "Approaching 52W High",
            "direction": "CALL", "tier": 2, "prob": prob,
            "swing_low": consol_low, "swing_high": high_52w,
            "retrace": retrace, "extend": extend,
            "near_fib_val": nfv, "near_fib_r": nfr, "fib_dist": nfd,
            "vol_vs_avg": round(consol_vol / avg_vol, 2),
            "days_since": 0, "earn_gap": 0, "stage": stage,
            "pct_from_pivot": round(pct_from_high, 2),
            "notes": "{:.1f}% from 52w high ${:.2f} | {} | Stage {}".format(
                pct_from_high, high_52w,
                "Vol contracting" if vol_contracting else "Watch vol", stage)}


def watch_earnings_gap_holding(daily, weekly=None):
    if not daily or len(daily) < 20:
        return None
    avg_vol = _avg_vol(daily, 30)
    price   = daily[-1]["c"]
    earn_idx = None
    for i in range(len(daily) - 25, len(daily) - 2):
        b, prev = daily[i], daily[i-1]
        gap_pct = (b["o"] - prev["c"]) / (prev["c"] or 1) * 100
        if b["v"] / (avg_vol or 1) >= 1.8 and gap_pct >= 2.5:
            earn_idx  = i
            earn_gap  = gap_pct
            earn_open = b["o"]
            break
    if earn_idx is None:
        return None
    days_since = len(daily) - 1 - earn_idx
    if not (3 <= days_since <= 30):
        return None
    if price < earn_open * 0.97:
        return None
    post_bars = daily[earn_idx:]
    pre_bars  = daily[max(0, earn_idx-10):earn_idx]
    if len(post_bars) < 3 or len(pre_bars) < 3:
        return None
    atr_post = _atr(post_bars, min(10, len(post_bars)))
    atr_pre  = _atr(pre_bars,  min(10, len(pre_bars)))
    if atr_pre > 0 and atr_post / atr_pre > 1.8:
        return None
    stage, _ = weinstein_stage(daily, weekly)
    if stage == 4:
        return None
    prob  = 45
    prob += 5 if days_since <= 10 else 0
    prob += 5 if earn_gap > 5.0 else 0
    prob += 5 if atr_pre > 0 and atr_post / atr_pre < 1.2 else 0
    prob  = min(prob, 58)
    pre_earn   = daily[max(0, earn_idx-10):earn_idx]
    swing_low  = min(b["l"] for b in pre_earn) if pre_earn else daily[earn_idx]["l"]
    swing_high = max(b["h"] for b in daily[earn_idx:])
    retrace, extend = fibonacci_levels(swing_low, swing_high, "CALL")
    nfv, nfr, nfd   = nearest_fib(price, retrace)
    return {"signal_type": "WATCH_EARNINGS", "signal_label": "Earnings Gap Holding",
            "direction": "CALL", "tier": 2, "prob": prob,
            "swing_low": swing_low, "swing_high": swing_high,
            "retrace": retrace, "extend": extend,
            "near_fib_val": nfv, "near_fib_r": nfr, "fib_dist": nfd,
            "vol_vs_avg": round(statistics.mean([b["v"] for b in daily[-5:]]) / (avg_vol or 1), 2),
            "days_since": days_since, "earn_gap": round(earn_gap, 2), "stage": stage,
            "pct_from_pivot": 0,
            "notes": "Gap {:+.1f}% | {} days ago | Holding above ${:.2f}".format(
                earn_gap, days_since, earn_open)}

def run_swing_scan():
    """
    Scan SWING_SYMBOLS concurrently (batches of 8).
    Updates both swing_signals (used by renderer) and all_swing_signals (used by chat).
    """
    global swing_signals, next_swing_scan, all_swing_signals, swing_next_scan_at
    log("=== Swing scan starting ({} symbols) ===".format(len(SWING_SYMBOLS)))

    results      = []
    results_lock = threading.Lock()

    def worker(sym):
        if sym in ("SPY",):
            return
        sig = scan_swing_symbol(sym)
        if sig:
            with results_lock:
                results.append(sig)

    for i in range(0, len(SWING_SYMBOLS), 8):
        batch   = SWING_SYMBOLS[i:i + 8]
        threads = [threading.Thread(target=worker, args=(s,), daemon=True) for s in batch]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=25)
        time.sleep(0.3)

    type_pri = {"ONEIL_PIVOT": 4, "WYCKOFF_SPRING": 3, "HI52_BREAKOUT": 2, "EARNINGS_CONT": 1}
    results.sort(key=lambda x: (-x["prob"], -type_pri.get(x["signal_type"], 0)))

    with state_lock:
        swing_signals      = results
        next_swing_scan    = time.time() + SWING_SCAN_INTERVAL
        all_swing_signals  = results
        swing_next_scan_at = next_swing_scan

    log("Swing scan done: {} setups found".format(len(results)))


def render_swing_dashboard():
    """Render the swing trade scanner page."""
    with state_lock:
        sigs = list(swing_signals)
        secs = max(0, int(next_swing_scan - time.time()))

    type_labels = {
        "ONEIL_PIVOT":    ("O'NEIL PIVOT",   "#2d1b69", "#a371f7"),
        "WYCKOFF_SPRING": ("WYCKOFF SPRING", "#1a3a2a", "#3fb950"),
        "HI52_BREAKOUT":  ("52W BREAKOUT",   "#1f3d5c", "#58a6ff"),
        "EARNINGS_CONT":  ("EARN CONT",      "#3d2800", "#e3b341"),
        "WATCH_ONEIL":    ("BASE FORMING",   "#1e1e2e", "#8b5cf6"),
        "WATCH_WYCKOFF":  ("ACCUM ZONE",     "#0d2010", "#22c55e"),
        "WATCH_52W":      ("NEAR 52W HIGH",  "#0d2038", "#38bdf8"),
        "WATCH_EARNINGS": ("GAP HOLDING",    "#2a1800", "#fb923c"),
    }


    tier1_render = [s for s in sigs if s.get("tier", 1) == 1]
    tier2_render = [s for s in sigs if s.get("tier", 2) == 2]

    t1_cards = ""
    for s in tier1_render:
        cards_html = ""
        prob      = s["prob"]
        direction = s["direction"]
        price     = s["price"]
        symbol    = s["symbol"]
        sig_type  = s.get("signal_type", "")
        sig_label = s.get("signal_label", sig_type)
        notes     = s.get("notes", "")
        stage     = s.get("stage")
        opt       = s.get("option") or {}

        # Probability color
        if prob >= 70:
            prob_color = "#3fb950"
        elif prob >= 55:
            prob_color = "#e3b341"
        else:
            prob_color = "#f85149"

        dir_color = "#3fb950" if direction == "CALL" else "#f85149"
        dir_arrow = "&#9650;" if direction == "CALL" else "&#9660;"

        # Signal type badge (primary)
        lbl, bg, fg = type_labels.get(sig_type, (sig_type, "#21262d", "#8b949e"))
        badges = ("<span style='background:{};color:{};font-size:9px;font-weight:700;"
                  "text-transform:uppercase;letter-spacing:.6px;padding:2px 7px;"
                  "border-radius:3px;margin-right:4px'>{}</span>").format(bg, fg, lbl)

        # Additional signal badges
        for extra_type in s.get("all_types", []):
            if extra_type == sig_type:
                continue
            el, ebg, efg = type_labels.get(extra_type, (extra_type, "#21262d", "#8b949e"))
            badges += ("<span style='background:{};color:{};font-size:9px;font-weight:600;"
                       "padding:2px 6px;border-radius:3px;margin-right:3px;opacity:.7'>{}</span>"
                       ).format(ebg, efg, el)

        # Stage badge
        stage_badge = ""
        if stage:
            sc = "#3fb950" if stage == 2 else "#e3b341" if stage == 1 else "#f85149"
            stage_badge = ("<span style='background:#0d1117;color:{};font-size:9px;"
                           "font-weight:700;padding:2px 7px;border-radius:3px;"
                           "border:1px solid {};margin-right:4px'>STAGE {}</span>"
                           ).format(sc, sc, stage)

        # Fib extensions block
        extend = s.get("extend", {})
        fib_html = ""
        if extend:
            if direction == "CALL":
                ext_items = sorted([(k, v) for k, v in extend.items() if v > price],
                                   key=lambda x: x[1])[:3]
            else:
                ext_items = sorted([(k, v) for k, v in extend.items() if v < price],
                                   key=lambda x: x[1], reverse=True)[:3]
            if ext_items:
                fib_lines = "".join(
                    "<div style='display:flex;justify-content:space-between'>"
                    "<span style='color:#8b949e'>{} ext</span>"
                    "<span style='color:#e6edf3;font-family:monospace'>${:.2f}</span></div>"
                    .format(k, v) for k, v in ext_items)
                fib_html = """
<div style='background:#0d1117;border-radius:6px;padding:8px 10px;margin-bottom:8px'>
  <div style='font-size:9px;color:#8b949e;text-transform:uppercase;
              letter-spacing:.6px;margin-bottom:5px'>Fibonacci Extension Targets</div>
  <div style='font-size:11px'>{fib_lines}</div>
  <div style='font-size:10px;color:#8b949e;margin-top:4px'>
    Base: ${low:.2f} &ndash; ${high:.2f}
  </div>
</div>""".format(
                    fib_lines = fib_lines,
                    low       = s.get("swing_low")  or 0,
                    high      = s.get("swing_high") or 0)

        # Option section
        opt_html = ""
        if opt.get("premium"):
            exp_short = (opt.get("expiry") or "")[-5:].replace("-", "/")
            prem      = opt["premium"]
            opt_html = """
<div style='background:#0d1117;border-radius:6px;padding:8px 10px;
            border:1px solid #238636;margin-bottom:8px'>
  <div style='font-size:9px;color:#8b949e;text-transform:uppercase;
              letter-spacing:.6px;margin-bottom:4px'>
    Recommended Option &nbsp;
    <span style='color:#e3b341'>{dte} DTE &nbsp; exp {exp}</span>
  </div>
  <div style='display:flex;justify-content:space-between;align-items:center'>
    <div>
      <span style='font-size:16px;font-weight:700;font-family:monospace'>${prem:.2f}</span>
      <span style='font-size:10px;color:#8b949e;margin-left:6px'>premium</span>
    </div>
    <div style='text-align:right;font-size:11px'>
      <div style='color:#e6edf3'>Strike ${strike} {dir}</div>
      <div style='color:#8b949e'>Delta {delta:.2f} &nbsp; IV {iv}%</div>
    </div>
  </div>
  <div style='font-size:10px;color:#8b949e;margin-top:4px'>
    Stop if premium &lt; <span style='color:#f85149'>${stp:.2f}</span>
    &nbsp;&nbsp; Target 2x <span style='color:#3fb950'>${tgt:.2f}</span>
    &nbsp;&nbsp; Bid/Ask ${bid}/{ask}
  </div>
</div>""".format(
                dte    = opt.get("dte", "?"),
                exp    = exp_short,
                prem   = prem,
                strike = opt.get("strike", "?"),
                dir    = direction,
                delta  = opt.get("delta") or 0,
                iv     = opt.get("iv") or 0,
                stp    = round(prem * 0.45, 2),
                tgt    = round(prem * 2.0,  2),
                bid    = opt.get("bid", "-"),
                ask    = opt.get("ask", "-"))
        else:
            opt_html = ("<div style='background:#0d1117;border-radius:6px;padding:8px 10px;"
                        "border:1px solid #30363d;font-size:11px;color:#8b949e;"
                        "margin-bottom:8px'>No options data &mdash; "
                        "check broker for 2-week {dir} expiry</div>").format(dir=direction)

        # RS badge
        rs     = s.get("spy_rs") or 0
        rs_col = "#3fb950" if rs > 0 else "#f85149"
        rs_txt = "{:+.1f}% vs SPY".format(rs)

        # R:R badge
        rr1    = s.get("rr1") or 0
        rr_col = "#3fb950" if rr1 >= 2 else "#e3b341" if rr1 >= 1 else "#8b949e"

        cards_html += """
<div style='background:#161b22;border:1px solid #30363d;border-radius:10px;
            margin-bottom:14px;padding:14px'>
  <div style='display:flex;justify-content:space-between;align-items:flex-start;
              margin-bottom:10px'>
    <div>
      <span style='font-size:20px;font-weight:800;letter-spacing:-.3px'>{sym}</span>
      <span style='color:{dc};font-size:13px;font-weight:700;margin-left:8px'>
        {darrow} {direction}
      </span>
      <div style='margin-top:6px;display:flex;flex-wrap:wrap;gap:3px;align-items:center'>
        {badges}
        {stage_badge}
      </div>
    </div>
    <div style='text-align:right'>
      <div style='font-size:26px;font-weight:800;color:{pc};line-height:1'>{prob}%</div>
      <div style='font-size:9px;color:#8b949e;text-transform:uppercase;
                  letter-spacing:.5px'>probability</div>
      <div style='font-size:10px;margin-top:3px'>
        <span style='color:{rrc}'>{rr:.1f}:1 R:R</span>
        &nbsp; <span style='color:{rsc}'>{rs_txt}</span>
      </div>
    </div>
  </div>

  <div style='display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:10px'>
    <div style='background:#0d1117;border-radius:6px;padding:8px'>
      <div style='font-size:9px;color:#8b949e;text-transform:uppercase;
                  letter-spacing:.5px;margin-bottom:3px'>Price</div>
      <div style='font-size:15px;font-weight:700;font-family:monospace'>${price}</div>
      <div style='font-size:10px;color:#f85149;margin-top:2px'>Stop ${stop}</div>
    </div>
    <div style='background:#0d1117;border-radius:6px;padding:8px'>
      <div style='font-size:9px;color:#8b949e;text-transform:uppercase;
                  letter-spacing:.5px;margin-bottom:3px'>Fib Targets</div>
      <div style='font-size:11px;line-height:1.7'>
        <span style='color:#8b949e'>T1 </span>
        <span style='color:#58a6ff;font-family:monospace'>${t1}</span><br>
        <span style='color:#8b949e'>T2 </span>
        <span style='color:#a371f7;font-family:monospace'>${t2}</span><br>
        <span style='color:#8b949e'>T3 </span>
        <span style='color:#3fb950;font-family:monospace'>${t3}</span>
      </div>
    </div>
    <div style='background:#0d1117;border-radius:6px;padding:8px'>
      <div style='font-size:9px;color:#8b949e;text-transform:uppercase;
                  letter-spacing:.5px;margin-bottom:3px'>Vol / Near Fib</div>
      <div style='font-size:12px;font-weight:700;color:#e3b341'>
        {vol}x avg
      </div>
      <div style='font-size:10px;color:#8b949e;margin-top:2px'>
        {nfr} level &nbsp; {nfd}% away
      </div>
    </div>
  </div>

  {fib_html}
  {opt_html}

  <div style='font-size:10px;color:#8b949e;border-top:1px solid #21262d;padding-top:8px'>
    {notes}
  </div>
</div>""".format(
            sym        = symbol,
            dc         = dir_color,
            darrow     = dir_arrow,
            direction  = direction,
            badges     = badges,
            stage_badge= stage_badge,
            prob       = prob,
            pc         = prob_color,
            rr         = rr1,
            rrc        = rr_col,
            rs_txt     = rs_txt,
            rsc        = rs_col,
            price      = price,
            stop       = s.get("stop", "?"),
            t1         = s.get("t1") or "-",
            t2         = s.get("t2") or "-",
            t3         = s.get("t3") or "-",
            vol        = s.get("vol_vs_avg") or "-",
            nfr        = s.get("near_fib_r") or "-",
            nfd        = s.get("fib_dist")   or "-",
            fib_html   = fib_html,
            opt_html   = opt_html,
            notes      = notes,
        )
        t1_cards += cards_html

    t2_cards = ""
    for s in tier2_render:
        cards_html = ""
        prob      = s["prob"]
        direction = s["direction"]
        price     = s["price"]
        symbol    = s["symbol"]
        sig_type  = s.get("signal_type", "")
        sig_label = s.get("signal_label", sig_type)
        notes     = s.get("notes", "")
        stage     = s.get("stage")
        opt       = s.get("option") or {}

        # Probability color
        if prob >= 70:
            prob_color = "#3fb950"
        elif prob >= 55:
            prob_color = "#e3b341"
        else:
            prob_color = "#f85149"

        dir_color = "#3fb950" if direction == "CALL" else "#f85149"
        dir_arrow = "&#9650;" if direction == "CALL" else "&#9660;"

        # Signal type badge (primary)
        lbl, bg, fg = type_labels.get(sig_type, (sig_type, "#21262d", "#8b949e"))
        badges = ("<span style='background:{};color:{};font-size:9px;font-weight:700;"
                  "text-transform:uppercase;letter-spacing:.6px;padding:2px 7px;"
                  "border-radius:3px;margin-right:4px'>{}</span>").format(bg, fg, lbl)

        # Additional signal badges
        for extra_type in s.get("all_types", []):
            if extra_type == sig_type:
                continue
            el, ebg, efg = type_labels.get(extra_type, (extra_type, "#21262d", "#8b949e"))
            badges += ("<span style='background:{};color:{};font-size:9px;font-weight:600;"
                       "padding:2px 6px;border-radius:3px;margin-right:3px;opacity:.7'>{}</span>"
                       ).format(ebg, efg, el)

        # Stage badge
        stage_badge = ""
        if stage:
            sc = "#3fb950" if stage == 2 else "#e3b341" if stage == 1 else "#f85149"
            stage_badge = ("<span style='background:#0d1117;color:{};font-size:9px;"
                           "font-weight:700;padding:2px 7px;border-radius:3px;"
                           "border:1px solid {};margin-right:4px'>STAGE {}</span>"
                           ).format(sc, sc, stage)

        # Fib extensions block
        extend = s.get("extend", {})
        fib_html = ""
        if extend:
            if direction == "CALL":
                ext_items = sorted([(k, v) for k, v in extend.items() if v > price],
                                   key=lambda x: x[1])[:3]
            else:
                ext_items = sorted([(k, v) for k, v in extend.items() if v < price],
                                   key=lambda x: x[1], reverse=True)[:3]
            if ext_items:
                fib_lines = "".join(
                    "<div style='display:flex;justify-content:space-between'>"
                    "<span style='color:#8b949e'>{} ext</span>"
                    "<span style='color:#e6edf3;font-family:monospace'>${:.2f}</span></div>"
                    .format(k, v) for k, v in ext_items)
                fib_html = """
<div style='background:#0d1117;border-radius:6px;padding:8px 10px;margin-bottom:8px'>
  <div style='font-size:9px;color:#8b949e;text-transform:uppercase;
              letter-spacing:.6px;margin-bottom:5px'>Fibonacci Extension Targets</div>
  <div style='font-size:11px'>{fib_lines}</div>
  <div style='font-size:10px;color:#8b949e;margin-top:4px'>
    Base: ${low:.2f} &ndash; ${high:.2f}
  </div>
</div>""".format(
                    fib_lines = fib_lines,
                    low       = s.get("swing_low")  or 0,
                    high      = s.get("swing_high") or 0)

        # Option section
        opt_html = ""
        if opt.get("premium"):
            exp_short = (opt.get("expiry") or "")[-5:].replace("-", "/")
            prem      = opt["premium"]
            opt_html = """
<div style='background:#0d1117;border-radius:6px;padding:8px 10px;
            border:1px solid #238636;margin-bottom:8px'>
  <div style='font-size:9px;color:#8b949e;text-transform:uppercase;
              letter-spacing:.6px;margin-bottom:4px'>
    Recommended Option &nbsp;
    <span style='color:#e3b341'>{dte} DTE &nbsp; exp {exp}</span>
  </div>
  <div style='display:flex;justify-content:space-between;align-items:center'>
    <div>
      <span style='font-size:16px;font-weight:700;font-family:monospace'>${prem:.2f}</span>
      <span style='font-size:10px;color:#8b949e;margin-left:6px'>premium</span>
    </div>
    <div style='text-align:right;font-size:11px'>
      <div style='color:#e6edf3'>Strike ${strike} {dir}</div>
      <div style='color:#8b949e'>Delta {delta:.2f} &nbsp; IV {iv}%</div>
    </div>
  </div>
  <div style='font-size:10px;color:#8b949e;margin-top:4px'>
    Stop if premium &lt; <span style='color:#f85149'>${stp:.2f}</span>
    &nbsp;&nbsp; Target 2x <span style='color:#3fb950'>${tgt:.2f}</span>
    &nbsp;&nbsp; Bid/Ask ${bid}/{ask}
  </div>
</div>""".format(
                dte    = opt.get("dte", "?"),
                exp    = exp_short,
                prem   = prem,
                strike = opt.get("strike", "?"),
                dir    = direction,
                delta  = opt.get("delta") or 0,
                iv     = opt.get("iv") or 0,
                stp    = round(prem * 0.45, 2),
                tgt    = round(prem * 2.0,  2),
                bid    = opt.get("bid", "-"),
                ask    = opt.get("ask", "-"))
        else:
            opt_html = ("<div style='background:#0d1117;border-radius:6px;padding:8px 10px;"
                        "border:1px solid #30363d;font-size:11px;color:#8b949e;"
                        "margin-bottom:8px'>No options data &mdash; "
                        "check broker for 2-week {dir} expiry</div>").format(dir=direction)

        # RS badge
        rs     = s.get("spy_rs") or 0
        rs_col = "#3fb950" if rs > 0 else "#f85149"
        rs_txt = "{:+.1f}% vs SPY".format(rs)

        # R:R badge
        rr1    = s.get("rr1") or 0
        rr_col = "#3fb950" if rr1 >= 2 else "#e3b341" if rr1 >= 1 else "#8b949e"

        cards_html += """
<div style='background:#161b22;border:1px solid #30363d;border-radius:10px;
            margin-bottom:14px;padding:14px'>
  <div style='display:flex;justify-content:space-between;align-items:flex-start;
              margin-bottom:10px'>
    <div>
      <span style='font-size:20px;font-weight:800;letter-spacing:-.3px'>{sym}</span>
      <span style='color:{dc};font-size:13px;font-weight:700;margin-left:8px'>
        {darrow} {direction}
      </span>
      <div style='margin-top:6px;display:flex;flex-wrap:wrap;gap:3px;align-items:center'>
        {badges}
        {stage_badge}
      </div>
    </div>
    <div style='text-align:right'>
      <div style='font-size:26px;font-weight:800;color:{pc};line-height:1'>{prob}%</div>
      <div style='font-size:9px;color:#8b949e;text-transform:uppercase;
                  letter-spacing:.5px'>probability</div>
      <div style='font-size:10px;margin-top:3px'>
        <span style='color:{rrc}'>{rr:.1f}:1 R:R</span>
        &nbsp; <span style='color:{rsc}'>{rs_txt}</span>
      </div>
    </div>
  </div>

  <div style='display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:10px'>
    <div style='background:#0d1117;border-radius:6px;padding:8px'>
      <div style='font-size:9px;color:#8b949e;text-transform:uppercase;
                  letter-spacing:.5px;margin-bottom:3px'>Price</div>
      <div style='font-size:15px;font-weight:700;font-family:monospace'>${price}</div>
      <div style='font-size:10px;color:#f85149;margin-top:2px'>Stop ${stop}</div>
    </div>
    <div style='background:#0d1117;border-radius:6px;padding:8px'>
      <div style='font-size:9px;color:#8b949e;text-transform:uppercase;
                  letter-spacing:.5px;margin-bottom:3px'>Fib Targets</div>
      <div style='font-size:11px;line-height:1.7'>
        <span style='color:#8b949e'>T1 </span>
        <span style='color:#58a6ff;font-family:monospace'>${t1}</span><br>
        <span style='color:#8b949e'>T2 </span>
        <span style='color:#a371f7;font-family:monospace'>${t2}</span><br>
        <span style='color:#8b949e'>T3 </span>
        <span style='color:#3fb950;font-family:monospace'>${t3}</span>
      </div>
    </div>
    <div style='background:#0d1117;border-radius:6px;padding:8px'>
      <div style='font-size:9px;color:#8b949e;text-transform:uppercase;
                  letter-spacing:.5px;margin-bottom:3px'>Vol / Near Fib</div>
      <div style='font-size:12px;font-weight:700;color:#e3b341'>
        {vol}x avg
      </div>
      <div style='font-size:10px;color:#8b949e;margin-top:2px'>
        {nfr} level &nbsp; {nfd}% away
      </div>
    </div>
  </div>

  {fib_html}
  {opt_html}

  <div style='font-size:10px;color:#8b949e;border-top:1px solid #21262d;padding-top:8px'>
    {notes}
  </div>
</div>""".format(
            sym        = symbol,
            dc         = dir_color,
            darrow     = dir_arrow,
            direction  = direction,
            badges     = badges,
            stage_badge= stage_badge,
            prob       = prob,
            pc         = prob_color,
            rr         = rr1,
            rrc        = rr_col,
            rs_txt     = rs_txt,
            rsc        = rs_col,
            price      = price,
            stop       = s.get("stop", "?"),
            t1         = s.get("t1") or "-",
            t2         = s.get("t2") or "-",
            t3         = s.get("t3") or "-",
            vol        = s.get("vol_vs_avg") or "-",
            nfr        = s.get("near_fib_r") or "-",
            nfd        = s.get("fib_dist")   or "-",
            fib_html   = fib_html,
            opt_html   = opt_html,
            notes      = notes,
        )
        t2_cards += cards_html

    t2_section = ""
    if t2_cards:
        t2_section = (
            "<div style='margin-top:24px'>"
            "<div style='font-size:10px;font-weight:700;color:#8b949e;"
            "text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px'>"
            "WATCHLIST &mdash; {n} PATTERNS FORMING"
            "</div>"
            "<div style='font-size:10px;color:#8b949e;margin-bottom:10px'>"
            "Not yet triggered &mdash; wait for the breakout / spring before entering."
            "</div><div style='opacity:.82'>{body}</div></div>"
        ).format(n=len(tier2_render), body=t2_cards)

    cards_html = t1_cards + t2_section

    if not cards_html:
        cards_html = ("<div style='padding:40px;text-align:center;color:#8b949e;font-size:13px'>"
                      "Swing scan running... check back in a moment.<br>"
                      "<a href='/swing' style='color:#58a6ff;font-size:11px;margin-top:8px;"
                      "display:block'>Refresh</a></div>")

    next_str = "{}s".format(secs) if secs > 0 else "running now"

    return """<!DOCTYPE html><html><head>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<meta http-equiv='refresh' content='120'>
<title>Swing Scanner</title>
<style>
body{{background:#0d1117;color:#e6edf3;font-family:-apple-system,Arial,sans-serif;
     padding:0;margin:0;font-size:13px}}
.topbar{{position:sticky;top:0;background:#161b22;border-bottom:1px solid #30363d;
         padding:10px 14px;display:flex;justify-content:space-between;
         align-items:center;z-index:100}}
.nav-link{{color:#58a6ff;text-decoration:none;font-size:11px;font-weight:600;
           padding:4px 10px;border-radius:5px;border:1px solid #30363d;margin-left:4px}}
.nav-link.active{{background:#1f6feb;border-color:#1f6feb;color:#fff}}
.content{{padding:12px 14px}}
</style></head><body>
<div class='topbar'>
  <div style='font-size:14px;font-weight:800;color:#58a6ff;letter-spacing:-.3px'>
    SWING ENGINE
    <span style='font-size:10px;color:#8b949e;font-weight:400;margin-left:6px'>
      {nsig} setups &nbsp;|&nbsp; next scan {next}
    </span>
  </div>
  <div>
    <a class='nav-link' href='/'>0DTE</a>
    <a class='nav-link active' href='/swing'>Swing</a>
    <a class='nav-link' href='/chat'>Chat</a>
    <a class='nav-link' href='/ai'>AI</a>
    <a class='nav-link' href='/stats'>Stats</a>
  </div>
</div>

<div class='content'>
  <div style='font-size:10px;color:#8b949e;font-weight:700;text-transform:uppercase;
              letter-spacing:.8px;margin-bottom:4px;margin-top:4px'>
    {nsig} ACTIVE SETUP{pl} &mdash; RANKED BY CONTINUATION PROBABILITY
  </div>
  <div style='font-size:10px;color:#8b949e;margin-bottom:10px'>
    <span style='color:#a371f7'>&#9632; O'Neil Pivot</span> &nbsp;
    <span style='color:#3fb950'>&#9632; Wyckoff Spring</span> &nbsp;
    <span style='color:#58a6ff'>&#9632; 52-Week Breakout</span> &nbsp;
    <span style='color:#e3b341'>&#9632; Earnings Cont.</span>
    &nbsp;&nbsp;|&nbsp;&nbsp; All signals Stage-2 filtered &nbsp;|&nbsp;&nbsp;
    Stops at 0.618 fib &nbsp;|&nbsp;&nbsp; Delta ~0.55, 2-week options
  </div>
  {cards}
</div>
</body></html>""".format(
        nsig  = len(sigs),
        next  = next_str,
        pl    = "S" if len(sigs) != 1 else "",
        cards = cards_html,
    )


# =============================================
# END SWING SCANNER
# =============================================

def background_scheduler():
    global _ai_last_run_date, _ai_last_trade_cnt
    log("Background scheduler started")
    time.sleep(10)
    # Kick off first swing scan immediately in background (daily bars, non-blocking)
    threading.Thread(target=run_swing_scan, daemon=True).start()
    while True:
        try:
            run_signal_scan()

            # --- Swing scan (every 15 min) ---
            if time.time() >= next_swing_scan:
                threading.Thread(target=run_swing_scan, daemon=True).start()

            # --- AI improvement triggers ---
            et      = pytz.timezone("America/New_York")
            now_et  = datetime.now(et)
            today   = now_et.strftime("%Y-%m-%d")
            et_hour = now_et.hour + now_et.minute / 60.0

            # 1. End-of-day: run once after 4:05 PM ET
            if et_hour >= 16.08 and _ai_last_run_date != today:
                log("AI: End-of-day trigger")
                threading.Thread(
                    target=run_ai_improvement,
                    args=("end_of_day",),
                    daemon=True
                ).start()

            # 2. Intraday: run after every 5 new closed trades
            all_closed = db_get_all_closed_trades()
            n_closed   = len(all_closed)
            if (n_closed >= 5 and
                    n_closed - _ai_last_trade_cnt >= 5):
                log("AI: Intraday trigger ({} new trades)".format(
                    n_closed - _ai_last_trade_cnt))
                threading.Thread(
                    target=run_ai_improvement,
                    args=("intraday_5trades",),
                    daemon=True
                ).start()

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

def render_dashboard(toast=""):
    # Build toast banner if present
    toast_html = ""
    if toast:
        if toast.startswith("logged:"):
            detail    = toast[7:].replace("+", " ")
            toast_msg = "Trade logged: {}".format(detail)
            toast_bg  = "#238636"
        elif toast.startswith("closed:"):
            outcome   = toast[7:]
            toast_msg = "Trade closed: {}".format(outcome)
            toast_bg  = "#238636" if outcome == "WIN" else "#da3633"
        else:
            toast_msg = toast
            toast_bg  = "#1f6feb"
        toast_html = """
<div id='toast' style='position:fixed;top:60px;left:50%;transform:translateX(-50%);
     background:{bg};color:#fff;padding:10px 24px;border-radius:8px;
     font-size:13px;font-weight:700;z-index:999;box-shadow:0 4px 16px rgba(0,0,0,.4);
     letter-spacing:.3px'>
  {msg}
</div>
<script>
  setTimeout(function(){{
    var t = document.getElementById('toast');
    if(t) t.style.display='none';
  }}, 3000);
</script>""".format(bg=toast_bg, msg=toast_msg)
        # Escape any remaining braces so the outer dashboard .format() doesn't choke
        toast_html = toast_html.replace("{", "{{").replace("}", "}}")
    with state_lock:
        signals  = list(all_signals)
        secs     = max(0, int(next_scan_at - time.time()))
        logs     = list(debug_log[-20:])

    trades      = db_get_today_trades()
    open_trades = db_get_open_trades()
    closed      = [t for t in trades if t["outcome"] != "OPEN"]
    total_pnl   = round(sum(t["pnl"] or 0 for t in closed), 2)
    wins        = len([t for t in closed if t["outcome"] == "WIN"])
    losses      = len([t for t in closed if t["outcome"] == "LOSS"])
    win_rate    = round(wins / len(closed) * 100) if closed else 0

    is_open      = market_open()
    mkt_color    = "#3fb950" if is_open else "#f85149"
    mkt_label    = "MKT OPEN" if is_open else "MKT CLOSED"
    pnl_color    = "#3fb950" if total_pnl >= 0 else "#f85149"
    wr_color     = "#3fb950" if win_rate >= 55 else "#e3b341" if win_rate >= 45 else "#f85149"

    # Derive market bias from first signal that has it
    market_bias = "---"
    spy_chg     = 0.0
    for s in signals:
        if s.get("market_bias"):
            market_bias = s["market_bias"]
            spy_chg     = s.get("spy_chg") or 0.0
            break
    bias_color = "#3fb950" if market_bias == "BULL" else "#f85149" if market_bias == "BEAR" else "#e3b341"

    spy_chg = float(spy_chg) if spy_chg is not None else 0.0

    # - Build signal cards -
    active_signals  = [s for s in signals if s.get("status") in ("SIGNAL", "SIGNAL (no options)")]
    watching_list   = [s for s in signals if s.get("status") == "WATCHING"]

    signal_cards = ""
    for s in active_signals:
        sym    = s["symbol"]
        price  = s.get("price", "-")
        d      = s.get("direction", "")
        status = s.get("status", "")

        grade       = s.get("grade") or "-"
        grade_pts   = float(s.get("grade_pts") or 0)
        grade_color = s.get("grade_color") or "#8b949e"

        gap_pct  = float(s.get("gap_pct") or 0)
        gap_dir  = s.get("gap_dir") or "FLAT"
        rs       = float(s.get("rs") or 0)
        late     = bool(s.get("late_entry", False))
        aligned  = bool(s.get("aligned", True))
        t1_prob  = int(s.get("t1_prob") or 50)
        t2_prob  = int(s.get("t2_prob") or 25)

        # Direction colors and labels
        is_call  = (d == "CALL")
        dc       = "#3fb950" if is_call else "#f85149"
        dir_bg   = "rgba(63,185,80,0.08)" if is_call else "rgba(248,81,73,0.08)"
        dir_border = "#238636" if is_call else "#da3633"

        # Targets
        if is_call:
            t1   = s.get("und_call_t1", "-")
            t2   = s.get("und_call_t2", "-")
            stop = s.get("und_call_stop", "-")
            arr  = "&#9650;"
        else:
            t1   = s.get("und_put_t1", "-")
            t2   = s.get("und_put_t2", "-")
            stop = s.get("und_put_stop", "-")
            arr  = "&#9660;"

        t_color = dc
        s_color = "#f85149" if is_call else "#3fb950"

        # Gap / RS display
        gap_c   = "#3fb950" if gap_dir == "UP" else "#f85149" if gap_dir == "DOWN" else "#8b949e"
        rs_c    = "#3fb950" if rs >= 0 else "#f85149"
        gap_str = "{}{:.2f}%".format("+" if gap_pct >= 0 else "", gap_pct)

        # Clear air
        ca       = s.get("clear_air") or {}
        ca_t1    = ca.get("clear_to_t1", True)
        ca_t2    = ca.get("clear_to_t2", True)
        ca_ctx   = ca.get("context", "")
        if ca_t2:
            ca_icon  = "&#10003;"
            ca_col   = "#3fb950"
            ca_label = "Clear air to T2"
        elif ca_t1:
            ca_icon  = "&#9888;"
            ca_col   = "#e3b341"
            ca_label = ca_ctx[:45] if ca_ctx else "Clear to T1 only"
        else:
            ca_icon  = "&#9888;"
            ca_col   = "#f85149"
            ca_label = ca_ctx[:45] if ca_ctx else "Level blocks T1"

        # Contract recommendation
        rec       = s.get("rec_contract") or {}
        rec_str   = rec.get("rec_strike", "-")
        rec_desc  = rec.get("strike_desc", "ATM")
        rec_plo   = rec.get("prem_est_low", "-")
        rec_phi   = rec.get("prem_est_high", "-")

        # Trend + time vol from result
        trend_1hr    = str(s.get("trend_1hr") or "MIXED")
        trend_desc   = str(s.get("trend_desc") or "")
        trend_score  = float(s.get("trend_score") or 0.0)
        tv_lbl       = str(s.get("time_vol_lbl") or "N/A")
        tv_ratio     = float(s.get("time_vol_ratio") or 1.0)
        is_primary   = bool(s.get("is_primary", False))
        rank_score   = int(s.get("rank_score") or 0)

        # Trend confirmation vs direction (use d, not direction)
        if d == "CALL":
            trend_aligned = trend_1hr == "BULL"
            trend_opposes = trend_1hr == "BEAR"
        else:
            trend_aligned = trend_1hr == "BEAR"
            trend_opposes = trend_1hr == "BULL"

        trend_col = "#3fb950" if trend_aligned else "#f85149" if trend_opposes else "#8b949e"
        trend_icon = "&#9650;" if trend_1hr == "BULL" else "&#9660;" if trend_1hr == "BEAR" else "&#8212;"
        tv_col = "#3fb950" if tv_ratio >= 2.0 else "#e3b341" if tv_ratio >= 1.3 else "#8b949e" if tv_ratio >= 0.8 else "#f85149"

        # Badges
        sig_type_0dte = s.get("signal_type", "ORB")
        sig_type_colors = {
            "ORB":           ("#21262d", "#58a6ff"),
            "VWAP_TREND":    ("#0b3d1a", "#3fb950"),
            "VWAP_MEAN_REV": ("#3d1a00", "#e3b341"),
            "IB_EXTENSION":  ("#1a1a3d", "#a371f7"),
            "VWAP_RECLAIM":  ("#1a2d3d", "#79c0ff"),
        }
        stc_bg, stc_fg = sig_type_colors.get(sig_type_0dte, ("#21262d", "#8b949e"))
        sig_type_badge = ("<span style='background:{bg};color:{fg};padding:2px 7px;"
                          "border-radius:3px;font-size:9px;font-weight:700;"
                          "margin-left:6px;letter-spacing:.3px;border:1px solid {fg}'>"
                          "{lbl}</span>".format(
                              bg=stc_bg, fg=stc_fg,
                              lbl=sig_type_0dte.replace("_", " ")))

        primary_badge = ("<span style='background:#1f6feb;color:#fff;padding:2px 8px;"
                         "border-radius:3px;font-size:10px;font-weight:700;"
                         "margin-left:6px;letter-spacing:.3px'>PRIMARY</span>" if is_primary else "")
        late_badge = ("<span style='background:#9e6a03;color:#fff;padding:2px 6px;"
                      "border-radius:3px;font-size:10px;font-weight:600;"
                      "margin-left:6px'>LATE</span>" if late else "")
        ct_badge   = ("<span style='background:#3d1a00;color:#e3b341;padding:2px 7px;"
                      "border-radius:3px;font-size:10px;font-weight:700;"
                      "margin-left:6px'>CTR-TREND</span>" if not aligned else "")

        # Premium / action section
        has_options = (status == "SIGNAL")
        dte_lbl     = str(s.get("dte_label") or "0DTE")
        dte_color   = "#3fb950" if dte_lbl == "0DTE" else "#e3b341"

        if has_options:
            prem     = s.get("premium", "-")
            stp_opt  = s.get("stop", "-")
            tgt_opt  = s.get("target", "-")
            con      = s.get("contracts", 1)
            take_url = ("/take?sym={}&dir={}&prem={}&con={}&stp={}&tgt={}"
                        "&grade={}&gpts={}&gap={:.2f}&gdir={}&rs={:.2f}").format(
                sym, d, prem, con, stp_opt, tgt_opt,
                grade, grade_pts, gap_pct, gap_dir, rs)
            option_section = """
      <div style='display:flex;align-items:center;justify-content:space-between;
                  background:#0d1117;border-radius:6px;padding:10px 12px;margin-top:10px;
                  border:1px solid #238636'>
        <div>
          <div style='font-size:10px;color:#8b949e;text-transform:uppercase;
                      letter-spacing:.5px;margin-bottom:3px'>
            Option Premium
            <span style='color:{dte_color};margin-left:6px;font-weight:700'>{dte_lbl}</span>
          </div>
          <div style='font-size:18px;font-weight:700;font-family:monospace'>${prem}</div>
          <div style='font-size:10px;color:#8b949e;margin-top:2px'>
            Stop ${sopt} &nbsp;|&nbsp; Target ${topt} &nbsp;|&nbsp; {con}x contracts
          </div>
        </div>
        <a href='{url}' style='background:#238636;color:#fff;padding:10px 20px;
           border-radius:6px;text-decoration:none;font-size:13px;font-weight:700;
           letter-spacing:.3px;white-space:nowrap'>LOG TRADE</a>
      </div>""".format(
                prem=prem, sopt=stp_opt, topt=tgt_opt, con=con,
                url=take_url, dte_lbl=dte_lbl, dte_color=dte_color)
        else:
            option_section = """
      <div style='background:#0d1117;border-radius:6px;padding:10px 12px;
                  margin-top:10px;border:1px solid #30363d;
                  font-size:11px;color:#8b949e'>
        No live option data &mdash; enter manually at broker
      </div>"""

        signal_cards += """
<div style='background:#161b22;border:1px solid {dborder};border-radius:10px;
            margin-bottom:12px;overflow:hidden'>

  <!-- Card header -->
  <div style='background:{dbg};padding:12px 14px;display:flex;
              align-items:center;justify-content:space-between;
              border-bottom:1px solid {dborder}'>
    <div style='display:flex;align-items:center;gap:10px;flex-wrap:wrap'>
      <span style='font-size:18px;font-weight:800;letter-spacing:.5px'>{sym}</span>
      <span style='color:{dc};font-size:14px;font-weight:700'>{arr} {d}</span>
      {sig_type_badge}{primary_badge}{late_badge}{ct_badge}
    </div>
    <div style='display:flex;align-items:center;gap:14px'>
      <div style='text-align:right'>
        <div style='font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.4px'>Rank</div>
        <div style='font-size:14px;font-weight:700;color:#e6edf3'>{rank}</div>
      </div>
      <div style='text-align:center'>
        <div style='font-size:28px;font-weight:900;color:{gc};line-height:1'>{grade}</div>
        <div style='font-size:10px;color:#8b949e;margin-top:1px'>{gpts}pts</div>
      </div>
    </div>
  </div>

  <!-- Card body -->
  <div style='padding:12px 14px'>

    <!-- Row 1: Price + Targets + Context -->
    <div style='display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:10px'>

      <!-- Price -->
      <div style='background:#0d1117;border-radius:6px;padding:10px'>
        <div style='font-size:10px;color:#8b949e;text-transform:uppercase;
                    letter-spacing:.5px;margin-bottom:4px'>Underlying</div>
        <div style='font-size:22px;font-weight:700;font-family:monospace'>${price}</div>
        <div style='font-size:10px;color:#8b949e;margin-top:3px'>
          VWAP ${vwap} &nbsp;|&nbsp; {vs_vwap}
        </div>
      </div>

      <!-- Targets -->
      <div style='background:#0d1117;border-radius:6px;padding:10px'>
        <div style='font-size:10px;color:#8b949e;text-transform:uppercase;
                    letter-spacing:.5px;margin-bottom:6px'>Price Targets</div>
        <div style='font-size:12px;line-height:1.9;font-family:monospace'>
          <span style='color:{tc}'>T1</span>
          <span style='font-weight:600'> ${t1}</span>
          <span style='color:#8b949e;font-size:10px'> {t1p}%</span><br>
          <span style='color:{tc}'>T2</span>
          <span style='font-weight:600'> ${t2}</span>
          <span style='color:#8b949e;font-size:10px'> {t2p}%</span><br>
          <span style='color:{sc}'>ST</span>
          <span style='font-weight:600'> ${stop}</span>
        </div>
      </div>

      <!-- Context: Gap / RS / Trend / Vol -->
      <div style='background:#0d1117;border-radius:6px;padding:10px'>
        <div style='font-size:10px;color:#8b949e;text-transform:uppercase;
                    letter-spacing:.5px;margin-bottom:6px'>Context</div>
        <div style='font-size:12px;line-height:1.9;font-family:monospace'>
          <span style='color:#8b949e'>GAP</span>
          <span style='color:{gapc};font-weight:600'> {gap_str}</span><br>
          <span style='color:#8b949e'>R/S</span>
          <span style='color:{rsc};font-weight:600'> {rs:+.2f}%</span><br>
          <span style='color:#8b949e'>1HR</span>
          <span style='color:{trend_col};font-weight:600'> {trend_icon} {trend_1hr}</span>
        </div>
      </div>
    </div>

    <!-- Row 2: Key levels | Volume | Rec contract -->
    <div style='display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:0'>

      <!-- Clear air -->
      <div style='background:#0d1117;border-radius:6px;padding:10px;
                  border-left:3px solid {ca_col}'>
        <div style='font-size:10px;color:#8b949e;text-transform:uppercase;
                    letter-spacing:.5px;margin-bottom:4px'>Key Levels</div>
        <div style='font-size:12px;color:{ca_col};font-weight:600'>
          {ca_icon} {ca_label}
        </div>
      </div>

      <!-- Time-adjusted volume -->
      <div style='background:#0d1117;border-radius:6px;padding:10px;
                  border-left:3px solid {tv_col}'>
        <div style='font-size:10px;color:#8b949e;text-transform:uppercase;
                    letter-spacing:.5px;margin-bottom:4px'>Volume Signal</div>
        <div style='font-size:12px;color:{tv_col};font-weight:600'>
          {tv_lbl}
        </div>
        <div style='font-size:10px;color:#8b949e;margin-top:2px'>vs time-of-day avg</div>
      </div>

      <!-- Recommended contract -->
      <div style='background:#0d1117;border-radius:6px;padding:10px;
                  border-left:3px solid #58a6ff'>
        <div style='font-size:10px;color:#8b949e;text-transform:uppercase;
                    letter-spacing:.5px;margin-bottom:4px'>Rec Contract</div>
        <div style='font-size:13px;font-weight:700;font-family:monospace'>
          ${rec_str} {d} &nbsp;<span style='font-size:10px;color:#8b949e;font-weight:400'>{rec_desc}</span>
        </div>
        <div style='font-size:10px;color:#8b949e;margin-top:2px'>
          Delta ~0.40 &nbsp;|&nbsp; Est ${rec_plo}-${rec_phi}
        </div>
      </div>
    </div>

    {option_section}
  </div>
</div>""".format(
            sym=sym, d=d, arr=arr, dc=dc,
            dbg=dir_bg, dborder=dir_border,
            grade=grade, gpts=grade_pts, gc=grade_color,
            primary_badge=primary_badge, late_badge=late_badge, ct_badge=ct_badge,
            sig_type_badge=sig_type_badge,
            rank=rank_score,
            price=price,
            vwap=s.get("vwap","-"),
            vs_vwap=s.get("vs_vwap",""),
            t1=t1, t2=t2, stop=stop,
            tc=t_color, sc=s_color,
            t1p=t1_prob, t2p=t2_prob,
            gapc=gap_c, gap_str=gap_str,
            rsc=rs_c, rs=rs,
            spy=spy_chg,
            trend_col=trend_col, trend_icon=trend_icon, trend_1hr=trend_1hr,
            ca_col=ca_col, ca_icon=ca_icon, ca_label=ca_label,
            tv_col=tv_col, tv_lbl=tv_lbl,
            rec_str=rec_str, rec_desc=rec_desc,
            rec_plo=rec_plo, rec_phi=rec_phi,
            option_section=option_section
        )

    # - WATCHING compact rows -
    watch_rows = ""
    for s in watching_list:
        sym  = s["symbol"]
        d    = s.get("direction","")
        dc   = "#3fb950" if d=="CALL" else "#f85149"
        p    = s.get("price","-")
        rs   = s.get("rs") or 0
        rsc  = "#3fb950" if rs >= 0 else "#f85149"
        gap  = s.get("gap_pct") or 0
        gapc = "#3fb950" if (s.get("gap_dir")=="UP") else "#f85149" if (s.get("gap_dir")=="DOWN") else "#8b949e"
        orb  = s.get("orb_high","-") if d=="CALL" else s.get("orb_low","-")
        brk  = "above ${}" .format(orb) if d=="CALL" else "below ${}".format(orb)
        watch_rows += (
            "<tr style='border-bottom:1px solid #21262d'>"
            "<td style='padding:8px 10px;font-weight:700;font-size:13px'>{sym}</td>"
            "<td style='padding:8px 6px;color:{dc};font-size:12px;font-weight:600'>{d}</td>"
            "<td style='padding:8px 6px;font-family:monospace;font-size:12px'>${p}</td>"
            "<td style='padding:8px 6px;font-size:11px;color:#e3b341'>Break {brk}</td>"
            "<td style='padding:8px 6px;font-size:11px'>"
            "  Gap <span style='color:{gapc}'>{gap:+.2f}%</span>"
            "</td>"
            "<td style='padding:8px 6px;font-size:11px'>"
            "  RS <span style='color:{rsc}'>{rs:+.2f}%</span>"
            "</td>"
            "</tr>"
        ).format(sym=sym, d=d, dc=dc, p=p, brk=brk,
                 gap=gap, gapc=gapc, rs=rs, rsc=rsc)

    # - Open trades rows -
    open_rows = ""
    for t in open_trades:
        cp = get_current_price(t["symbol"])
        if cp and t["premium"]:
            unreal = round((cp - t["premium"]) * 100 * t["contracts"], 2)
            uc = "#3fb950" if unreal >= 0 else "#f85149"
            us = "<span style='color:{};font-weight:600'>${}</span>".format(uc, unreal)
        else:
            us = "<span style='color:#8b949e'>-</span>"
        dc = "#3fb950" if t["direction"]=="CALL" else "#f85149"
        open_rows += (
            "<tr style='border-bottom:1px solid #21262d'>"
            "<td style='padding:9px 10px;font-weight:700'>{sym}</td>"
            "<td style='padding:9px 6px;color:{dc}'>{dir}</td>"
            "<td style='padding:9px 6px;font-family:monospace'>${prem}</td>"
            "<td style='padding:9px 6px'>{con}x</td>"
            "<td style='padding:9px 6px'>{unreal}</td>"
            "<td style='padding:9px 6px'>"
            "<a href='/close?id={id}&outcome=WIN&exit={cp}' "
            "style='background:#238636;color:#fff;padding:4px 10px;"
            "border-radius:4px;text-decoration:none;font-size:11px;"
            "font-weight:600;margin-right:4px'>WIN</a>"
            "<a href='/close?id={id}&outcome=LOSS&exit={cp}' "
            "style='background:#da3633;color:#fff;padding:4px 10px;"
            "border-radius:4px;text-decoration:none;font-size:11px;"
            "font-weight:600'>LOSS</a>"
            "</td></tr>"
        ).format(sym=t["symbol"], dc=dc, dir=t["direction"],
                 prem=t["premium"], con=t["contracts"],
                 unreal=us, id=t["id"], cp=cp or 0)

    # - Closed trades rows -
    closed_rows = ""
    for t in closed:
        pc = "#3fb950" if (t["pnl"] or 0) >= 0 else "#f85149"
        oc = "#3fb950" if t["outcome"] == "WIN" else "#f85149"
        dc = "#3fb950" if t["direction"]=="CALL" else "#f85149"
        closed_rows += (
            "<tr style='border-bottom:1px solid #21262d'>"
            "<td style='padding:9px 10px;font-weight:700'>{sym}</td>"
            "<td style='padding:9px 6px;color:{dc}'>{dir}</td>"
            "<td style='padding:9px 6px;font-family:monospace'>${prem}</td>"
            "<td style='padding:9px 6px;color:{oc};font-weight:600'>{out}</td>"
            "<td style='padding:9px 6px;color:{pc};font-weight:600;font-family:monospace'>${pnl}</td>"
            "</tr>"
        ).format(sym=t["symbol"], dc=dc, dir=t["direction"],
                 prem=t["premium"], oc=oc, out=t["outcome"],
                 pc=pc, pnl=round(t["pnl"] or 0, 2))

    no_signals   = not active_signals
    no_watch     = not watching_list
    no_open      = not open_trades
    no_closed    = not closed

    # --- AI insight panel ---
    cfg_now     = get_config()
    ai_ver      = cfg_now.get("ai_version", 0)
    ai_insight  = cfg_now.get("ai_insight", "")
    ai_focus    = cfg_now.get("ai_focus", "")
    ai_updated  = cfg_now.get("updated_at", "default")

    if ai_ver > 0:
        ai_panel = """
<div style='margin:10px 14px 0;background:#161b22;border:1px solid #1f6feb;
            border-radius:8px;padding:10px 14px;
            border-left:3px solid #58a6ff'>
  <div style='display:flex;align-items:center;justify-content:space-between;
              margin-bottom:6px'>
    <div style='font-size:10px;font-weight:700;color:#58a6ff;
                text-transform:uppercase;letter-spacing:.6px'>
      AI Engine &mdash; Config v{ver} &mdash; Updated {upd}
    </div>
    <a href='/ai' style='font-size:10px;color:#58a6ff;text-decoration:none'>
      Full Analysis &#8594;
    </a>
  </div>
  <div style='font-size:12px;color:#e6edf3;margin-bottom:4px'>
    <span style='color:#8b949e'>Insight:</span> {insight}
  </div>
  <div style='font-size:12px;color:#e6edf3'>
    <span style='color:#8b949e'>Focus:</span> {focus}
  </div>
</div>""".format(ver=ai_ver, upd=ai_updated, insight=ai_insight, focus=ai_focus)
    else:
        ai_panel = """
<div style='margin:10px 14px 0;background:#161b22;border:1px solid #30363d;
            border-radius:8px;padding:10px 14px'>
  <div style='font-size:10px;font-weight:700;color:#8b949e;
              text-transform:uppercase;letter-spacing:.6px;margin-bottom:4px'>
    AI Engine &mdash; Collecting Data
  </div>
  <div style='font-size:12px;color:#8b949e'>
    {insight} &nbsp;
    <a href='/ai' style='color:#58a6ff;text-decoration:none'>View AI panel &#8594;</a>
  </div>
</div>""".format(insight=ai_insight)

    html = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>0DTE Engine</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0d1117;color:#e6edf3;
       font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
       font-size:13px;min-height:100vh}}
  .topbar{{background:#010409;border-bottom:1px solid #30363d;
           padding:10px 14px;display:flex;align-items:center;
           justify-content:space-between;flex-wrap:wrap;gap:8px;
           position:sticky;top:0;z-index:100}}
  .topbar-left{{display:flex;align-items:center;gap:16px;flex-wrap:wrap}}
  .topbar-right{{display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
  .brand{{font-size:13px;font-weight:700;color:#58a6ff;letter-spacing:.5px}}
  .stat-chip{{background:#161b22;border:1px solid #30363d;border-radius:6px;
              padding:4px 10px;font-size:11px;white-space:nowrap}}
  .stat-chip .val{{font-weight:700;font-size:13px}}
  .bias-bar{{padding:7px 14px;display:flex;align-items:center;gap:14px;
             border-bottom:1px solid #30363d;font-size:11px;flex-wrap:wrap}}
  .section-label{{font-size:10px;font-weight:700;color:#8b949e;
                  text-transform:uppercase;letter-spacing:.8px;
                  padding:12px 14px 6px;}}
  .empty{{padding:20px;text-align:center;color:#8b949e;font-size:12px}}
  .watch-table{{width:100%;border-collapse:collapse}}
  .watch-table th{{padding:7px 10px;text-align:left;font-size:10px;
                   font-weight:600;color:#8b949e;border-bottom:1px solid #30363d;
                   text-transform:uppercase;letter-spacing:.5px}}
  .trade-table{{width:100%;border-collapse:collapse}}
  .trade-table th{{padding:8px 10px;text-align:left;font-size:10px;
                   font-weight:600;color:#8b949e;border-bottom:1px solid #30363d;
                   text-transform:uppercase;letter-spacing:.5px}}
  .card-wrap{{padding:10px 14px}}
  .section-card{{background:#161b22;border:1px solid #30363d;
                 border-radius:10px;margin-bottom:12px;overflow:hidden}}
  .nav-link{{color:#58a6ff;text-decoration:none;font-size:11px;font-weight:500}}
  .nav-link:hover{{text-decoration:underline}}
  .dot{{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:4px}}
  .debug-box{{background:#010409;font-size:10px;font-family:monospace;
              max-height:160px;overflow-y:auto;color:#6e7681;
              line-height:1.6;padding:10px 14px}}
</style>
</head><body>

{toast_html}

<!-- TOP BAR -->
<div class="topbar">
  <div class="topbar-left">
    <span class="brand">0DTE ENGINE</span>
    <span class="stat-chip">
      <span class="dot" style="background:{mc}"></span>
      <span class="val" style="color:{mc}">{ml}</span>
    </span>
    <span class="stat-chip">
      Bias <span class="val" style="color:{biasc}">&nbsp;{bias}</span>
      <span style="color:#8b949e"> &nbsp;SPY {spy:+.2f}%</span>
    </span>
    <span class="stat-chip">
      P&amp;L <span class="val" style="color:{pc}">&nbsp;${pl}</span>
    </span>
    <span class="stat-chip">
      <span class="val" style="color:{wrc}">{wr}%</span>
      <span style="color:#8b949e"> &nbsp;{nw}W {nl}L</span>
    </span>
    <span class="stat-chip" style="color:#8b949e">
      Scan <span class="val" style="color:#e6edf3">&nbsp;{sc}s</span>
    </span>
  </div>
  <div class="topbar-right">
    <a class="nav-link" href="/swing">Swing</a>
    <a class="nav-link" href="/chat">Chat</a>
    <a class="nav-link" href="/ai">AI</a>
    <a class="nav-link" href="/stats">Stats</a>
    <a class="nav-link" href="/alpaca-test">Alpaca</a>
    <a class="nav-link" href="/debug">Debug</a>
  </div>
</div>

<!-- AI INSIGHT PANEL -->
{ai_panel}

<!-- SIGNAL CARDS -->
<div style="padding:12px 14px 0">
  <div style="font-size:10px;font-weight:700;color:#8b949e;
              text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px">
    Active Signals &mdash; {nsig} setup{pl_s} found
  </div>
  {signal_cards}
  {no_sig_msg}
</div>

<!-- WATCHING -->
<div class="section-label">Watching &mdash; {nwatch} setup{pl_w}</div>
<div class="section-card" style="margin:0 14px 12px">
  {watch_content}
</div>

<!-- OPEN TRADES -->
<div class="section-label">Open Trades</div>
<div class="section-card" style="margin:0 14px 12px">
  {open_content}
</div>

<!-- CLOSED TODAY -->
<div class="section-label">Closed Today</div>
<div class="section-card" style="margin:0 14px 12px">
  {closed_content}
</div>

<!-- DEBUG -->
<div class="section-label">Debug Log</div>
<div class="section-card" style="margin:0 14px 14px">
  <div class="debug-box">{log_lines}</div>
</div>

</body></html>""".format(
        toast_html=toast_html,
        mc=mkt_color, ml=mkt_label,
        ai_panel=ai_panel,
        bias=market_bias, biasc=bias_color, spy=spy_chg,
        pc=pnl_color, pl=total_pnl,
        wrc=wr_color, wr=win_rate, nw=wins, nl=losses,
        sc=secs,
        nsig=len(active_signals),
        pl_s="" if len(active_signals)==1 else "s",
        signal_cards=signal_cards,
        no_sig_msg=("<div style='background:#161b22;border:1px solid #30363d;"
                    "border-radius:10px;padding:24px;text-align:center;"
                    "color:#8b949e;font-size:12px'>"
                    "No signals confirmed yet &mdash; waiting for ORB breakout</div>"
                    if no_signals else ""),
        nwatch=len(watching_list),
        pl_w="" if len(watching_list)==1 else "s",
        watch_content=(
            "<table class='watch-table'>"
            "<tr><th>Symbol</th><th>Dir</th><th>Price</th>"
            "<th>Breakout Level</th><th>Gap</th><th>Rel Str</th></tr>"
            + watch_rows + "</table>"
            if not no_watch else
            "<div class='empty'>No symbols approaching ORB levels</div>"
        ),
        open_content=(
            "<table class='trade-table'>"
            "<tr><th>Symbol</th><th>Dir</th><th>Entry</th>"
            "<th>Size</th><th>Unreal P&amp;L</th><th>Close</th></tr>"
            + open_rows + "</table>"
            if not no_open else
            "<div class='empty'>No open trades</div>"
        ),
        closed_content=(
            "<table class='trade-table'>"
            "<tr><th>Symbol</th><th>Dir</th><th>Entry</th>"
            "<th>Result</th><th>P&amp;L</th></tr>"
            + closed_rows + "</table>"
            if not no_closed else
            "<div class='empty'>No closed trades today</div>"
        ),
        log_lines="<br>".join(logs) if logs else "No log entries yet"
    )
    return html


@app.route("/")
def home():
    try:
        toast = request.args.get("toast", "")
        return render_dashboard(toast=toast)
    except Exception as _e:
        import traceback
        tb = traceback.format_exc()
        return ("<pre style='background:#0d1117;color:#f85149;padding:20px;"
                "font-size:12px;white-space:pre-wrap'>"
                + tb + "</pre>"), 500


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
    return redirect("/?toast=logged:{}+{}".format(sym, dir_))


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
    return redirect("/?toast=closed:{}".format(outcome))


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


@app.route("/ai")
def ai_page():
    """Full AI analysis history, current config, and manual run trigger."""
    cfg       = get_config()
    analyses  = db_get_ai_analyses(limit=20)
    proposals = db_get_proposals(limit=30)

    pending   = [p for p in proposals if p["status"] == "pending"]
    dismissed = [p for p in proposals if p["status"] == "dismissed"]

    # Config table rows
    skip_keys = {"ai_insight", "ai_focus", "updated_at", "updated_by"}
    cfg_rows  = ""
    for k, v in sorted(cfg.items()):
        if k in skip_keys:
            continue
        cfg_rows += (
            "<tr style='border-bottom:1px solid #21262d'>"
            "<td style='padding:7px 10px;color:#8b949e;font-family:monospace'>{}</td>"
            "<td style='padding:7px 10px;font-weight:600;font-family:monospace'>{}</td>"
            "</tr>"
        ).format(k, v)

    # Analysis history rows
    analysis_rows = ""
    for a in analyses:
        diff  = json.loads(a["config_diff"]) if a["config_diff"] else {}
        diffs = ", ".join("{}: {}->{}".format(k, v["old"], v["new"])
                         for k, v in diff.items()) or "no changes"
        wr_c  = "#3fb950" if (a["win_rate"] or 0) >= 55 else "#e3b341" if (a["win_rate"] or 0) >= 45 else "#f85149"
        analysis_rows += """
<div style='background:#161b22;border:1px solid #30363d;border-radius:8px;
            padding:12px 14px;margin-bottom:10px'>
  <div style='display:flex;justify-content:space-between;align-items:center;
              margin-bottom:8px'>
    <div style='font-size:11px;color:#8b949e'>{ts}</div>
    <div>
      <span style='color:{wrc};font-weight:700'>{wr}% WR</span>
      <span style='color:#8b949e;font-size:11px'> &nbsp;{n} trades</span>
    </div>
  </div>
  <div style='font-size:13px;font-weight:600;margin-bottom:4px'>{insight}</div>
  <div style='font-size:12px;color:#e3b341;margin-bottom:6px'>{focus}</div>
  <div style='font-size:11px;color:#8b949e;margin-bottom:4px'>{reasoning}</div>
  <div style='font-size:10px;background:#0d1117;padding:6px 8px;border-radius:4px;
              font-family:monospace;color:#58a6ff'>Changes: {diffs}</div>
</div>""".format(
            ts=a["ts"][:16], wrc=wr_c, wr=a["win_rate"] or 0,
            n=a["trades_used"], insight=a["insight"] or "",
            focus=a["focus"] or "", reasoning=a["reasoning"] or "",
            diffs=diffs
        )

    if not analysis_rows:
        analysis_rows = ("<div style='padding:20px;text-align:center;color:#8b949e;font-size:12px'>"
                         "No AI analyses yet. Need 5+ closed trades to trigger first run."
                         "</div>")

    # --- Proposal cards ---
    type_colors = {
        "NEW_STRATEGY":  ("#1f6feb", "#58a6ff"),
        "NEW_INDICATOR": ("#1a472a", "#3fb950"),
        "NEW_FILTER":    ("#3d1a00", "#e3b341"),
        "NEW_SIZING":    ("#2d1b69", "#a371f7"),
        "DASHBOARD":     ("#1b2a3d", "#79c0ff"),
    }

    def proposal_card(p, show_dismiss=True):
        bg, fg   = type_colors.get(p["proposal_type"], ("#161b22", "#8b949e"))
        spec_id  = "spec_{}".format(p["id"])
        copy_id  = "copy_{}".format(p["id"])
        dismiss  = ""
        if show_dismiss:
            dismiss = ("<a href='/ai/dismiss?id={id}' style='color:#8b949e;"
                       "font-size:10px;text-decoration:none;margin-left:10px'>"
                       "dismiss</a>").format(id=p["id"])

        # Build the copyable brief for pasting to Claude
        paste_text = (
            "AI PROPOSAL: {title}\\n\\n"
            "Type: {ptype}\\n"
            "Summary: {summary}\\n\\n"
            "Evidence from trade data: {evidence}\\n\\n"
            "Implementation spec:\\n{spec}"
        ).format(
            title   = p["title"],
            ptype   = p["proposal_type"],
            summary = p["summary"],
            evidence= p["evidence"],
            spec    = p["spec"]
        ).replace("'", "\\'").replace("\n", "\\n")

        return """
<div style='background:#161b22;border:1px solid {bg};border-radius:8px;
            padding:14px;margin-bottom:10px;position:relative'>
  <div style='display:flex;align-items:flex-start;justify-content:space-between;
              margin-bottom:8px;gap:10px'>
    <div>
      <span style='background:{bg};color:{fg};font-size:9px;font-weight:700;
                   text-transform:uppercase;letter-spacing:.6px;padding:2px 8px;
                   border-radius:3px'>{ptype}</span>
      <span style='font-size:13px;font-weight:700;margin-left:8px'>{title}</span>
      {dismiss}
    </div>
    <div style='font-size:10px;color:#8b949e;white-space:nowrap'>{ts}</div>
  </div>
  <div style='font-size:12px;color:#e6edf3;margin-bottom:6px'>{summary}</div>
  <div style='font-size:11px;color:#8b949e;margin-bottom:10px'>
    <span style='color:#e3b341'>Evidence:</span> {evidence}
  </div>
  <details style='margin-bottom:10px'>
    <summary style='font-size:11px;color:#58a6ff;cursor:pointer;
                    list-style:none;margin-bottom:6px'>
      View implementation spec &#9660;
    </summary>
    <div id='{spec_id}' style='background:#0d1117;border-radius:6px;padding:10px;
                font-size:11px;color:#e6edf3;line-height:1.6;
                font-family:monospace;white-space:pre-wrap'>{spec}</div>
  </details>
  <button id='{copy_id}'
    onclick="
      var txt = '{paste}';
      txt = txt.replace(/\\\\n/g, '\\n');
      navigator.clipboard.writeText(txt).then(function(){{
        document.getElementById('{copy_id}').textContent = 'Copied!';
        setTimeout(function(){{
          document.getElementById('{copy_id}').textContent = 'Copy to send to Claude';
        }}, 2000);
      }});
    "
    style='background:#1f6feb;color:#fff;border:none;padding:8px 16px;
           border-radius:6px;font-size:12px;font-weight:700;cursor:pointer'>
    Copy to send to Claude
  </button>
</div>""".format(
            bg=bg, fg=fg,
            ptype  = p["proposal_type"],
            title  = p["title"],
            dismiss= dismiss,
            ts     = p["ts"][:16],
            summary= p["summary"],
            evidence=p["evidence"],
            spec_id= spec_id,
            spec   = p["spec"],
            copy_id= copy_id,
            paste  = paste_text
        )

    pending_html = "".join(proposal_card(p) for p in pending)
    if not pending_html:
        pending_html = ("<div style='padding:16px;text-align:center;color:#8b949e;"
                        "font-size:12px'>No pending proposals -- AI will generate "
                        "these after analyzing trade patterns.</div>")

    dismissed_html = ""
    if dismissed:
        dismissed_html = """
<h2 style='color:#8b949e'>Dismissed Proposals</h2>
{}""".format("".join(proposal_card(p, show_dismiss=False) for p in dismissed))

    can_run   = bool(ANTHROPIC_KEY)
    run_btn   = ""
    if can_run:
        run_btn = ("<a href='/ai/run' style='background:#1f6feb;color:#fff;"
                   "padding:8px 18px;border-radius:6px;text-decoration:none;"
                   "font-size:12px;font-weight:700'>Run AI Now</a>")
    else:
        run_btn = ("<span style='color:#f85149;font-size:12px'>"
                   "ANTHROPIC_API_KEY not set</span>")

    return """<!DOCTYPE html><html><head>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<style>
body{{background:#0d1117;color:#e6edf3;font-family:-apple-system,Arial,sans-serif;
     padding:14px;margin:0;font-size:13px}}
h2{{font-size:15px;color:#58a6ff;margin:18px 0 8px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:10px;
       margin-bottom:14px;overflow:hidden}}
.ch{{padding:10px 14px;border-bottom:1px solid #21262d;font-size:12px;
     font-weight:700;display:flex;align-items:center;justify-content:space-between}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{padding:7px 10px;text-align:left;color:#8b949e;border-bottom:1px solid #21262d;
    font-size:10px;text-transform:uppercase;letter-spacing:.5px}}
a.nav{{color:#58a6ff;text-decoration:none;font-size:12px}}
details summary::-webkit-details-marker {{ display:none }}
</style></head><body>
<div style='margin-bottom:12px;display:flex;align-items:center;gap:16px'>
  <a class='nav' href='/'>&#8592; Dashboard</a>
  {run_btn}
</div>

<h1 style='font-size:17px;margin-bottom:4px'>AI Optimization Engine</h1>
<div style='font-size:11px;color:#8b949e;margin-bottom:14px'>
  Config v{ver} &nbsp;|&nbsp; Updated by {by} at {upd} &nbsp;|&nbsp;
  <span style='color:#e3b341'>{insight}</span>
</div>

<h2>Structural Proposals
  <span style='font-size:11px;color:#8b949e;font-weight:400;margin-left:8px'>
    -- copy any proposal and paste it to Claude to implement
  </span>
</h2>
{pending_html}
{dismissed_html}

<h2>Current Config</h2>
<div class='card'>
  <table><tr><th>Parameter</th><th>Value</th></tr>{cfg_rows}</table>
</div>

<h2>Analysis History</h2>
{analyses}

</body></html>""".format(
        run_btn      = run_btn,
        ver          = cfg.get("ai_version", 0),
        by           = cfg.get("updated_by", "default"),
        upd          = cfg.get("updated_at", "never"),
        insight      = cfg.get("ai_insight", ""),
        pending_html = pending_html,
        dismissed_html=dismissed_html,
        cfg_rows     = cfg_rows,
        analyses     = analysis_rows
    )


@app.route("/ai/run")
def ai_run_manual():
    """Manual AI improvement trigger."""
    if not ANTHROPIC_KEY:
        return ("<html><body style='background:#0d1117;color:#f85149;"
                "font-family:Arial;padding:20px'>"
                "<h2>ANTHROPIC_API_KEY not set</h2>"
                "<a href='/ai' style='color:#58a6ff'>Back</a></body></html>")
    threading.Thread(
        target=run_ai_improvement,
        args=("manual",),
        daemon=True
    ).start()
    return redirect("/ai")


@app.route("/ai/dismiss")
def ai_dismiss_proposal():
    pid = request.args.get("id", "")
    if pid:
        try:
            db_dismiss_proposal(int(pid))
        except Exception as e:
            log("Dismiss proposal error: {}".format(e))
    return redirect("/ai")


@app.route("/swing")
def swing_page():
    try:
        return render_swing_dashboard()
    except Exception as e:
        import traceback
        return ("<pre style='background:#0d1117;color:#f85149;padding:20px;"
                "font-size:12px;white-space:pre-wrap'>" + traceback.format_exc() + "</pre>"), 500


@app.route("/swing/scan")
def swing_scan_now():
    """Manually trigger a swing scan."""
    threading.Thread(target=run_swing_scan, daemon=True).start()
    return redirect("/swing")


def _build_scanner_context():
    """Build a rich context string of current scanner state for the chat system prompt."""
    et     = pytz.timezone("America/New_York")
    now_et = datetime.now(et)
    cfg    = get_config()

    # 0DTE signals
    with state_lock:
        dte_sigs = list(all_signals)
    active = [s for s in dte_sigs if s.get("status") in ("SIGNAL", "SIGNAL (no options)")]
    watching = [s for s in dte_sigs if s.get("status") == "WATCHING"]

    dte_lines = []
    for s in active:
        dte_lines.append(
            "  {sym} {d} | Grade:{g}({gp}pts) | Price:{p} | VWAP:{vwap} | "
            "T1:{t1} T2:{t2} | Stop:{stp} | Gap:{gap:.2f}% | RS:{rs:.2f}% | "
            "1HR:{hr} | Vol:{vol} | Rank:{rank}".format(
                sym=s["symbol"], d=s.get("direction",""),
                g=s.get("grade","-"), gp=s.get("grade_pts",0),
                p=s.get("price",0), vwap=round(s.get("vwap",0),2),
                t1=s.get("t1",0), t2=s.get("t2",0),
                stp=s.get("stop_loss",0),
                gap=s.get("gap_pct",0), rs=s.get("rs",0),
                hr=s.get("trend_1hr","?"),
                vol=s.get("vol_signal","?"),
                rank=s.get("rank_score",0)
            ))

    # Swing signals
    with swing_lock:
        sw_sigs = list(all_swing_signals)

    sw_lines = []
    for s in sw_sigs[:20]:  # top 20
        opt = s.get("option") or {}
        sw_lines.append(
            "  {sym} {d} | {stype} | Prob:{prob}% | Price:{p} | "
            "T1:{t1} T2:{t2} T3:{t3} | Stop:{stp} | R:R {rr} | RS:{rs:+.1f}% | "
            "Option:{oprem} {dte}DTE".format(
                sym=s["symbol"], d=s["direction"],
                stype=s["signal_type"], prob=s["prob"],
                p=s["price"],
                t1=s.get("t1") or "-", t2=s.get("t2") or "-", t3=s.get("t3") or "-",
                stp=s.get("stop","-"), rr=s.get("rr1",0),
                rs=s.get("spy_rs",0),
                oprem=opt.get("premium","-") if opt else "-",
                dte=opt.get("dte","?") if opt else "?",
            ))

    # Today's trades
    trades     = db_get_today_trades()
    open_t     = db_get_open_trades()
    closed_t   = [t for t in trades if t["outcome"] != "OPEN"]
    wins       = len([t for t in closed_t if t["outcome"] == "WIN"])
    losses     = len([t for t in closed_t if t["outcome"] == "LOSS"])
    total_pnl  = round(sum(t["pnl"] or 0 for t in closed_t), 2)

    open_lines = []
    for t in open_t:
        open_lines.append(
            "  #{id} {sym} {d} | Entry:${e} x{c} | Stop:${stp} Target:${tgt}".format(
                id=t["id"], sym=t["symbol"], d=t["direction"],
                e=t["entry_price"], c=t["contracts"],
                stp=t["stop_price"], tgt=t["target_price"]))

    # Market
    bias = "---"
    spy_chg = 0.0
    for s in dte_sigs:
        if s.get("market_bias"):
            bias    = s["market_bias"]
            spy_chg = s.get("spy_chg", 0.0)
            break

    ctx = """=== SCANNER CONTEXT ({time} ET) ===

MARKET: {bias} | SPY {spy:+.2f}% | {mkt}

0DTE ACTIVE SIGNALS ({nact} setups):
{dte_active}

0DTE WATCHING ({nwatch} stocks):
{dte_watch}

SWING SETUPS (top 20 of {nsw} found):
{sw}

OPEN TRADES ({nopen}):
{open_t}

TODAY'S P&L: ${pnl} | {w}W {l}L

AI CONFIG: v{ver} | Insight: {insight} | Focus: {focus}
""".format(
        time    = now_et.strftime("%H:%M"),
        bias    = bias,
        spy     = float(spy_chg or 0),
        mkt     = "MARKET OPEN" if market_open() else "MARKET CLOSED",
        nact    = len(active),
        dte_active = "\n".join(dte_lines) or "  (none)",
        nwatch  = len(watching),
        dte_watch  = "  " + ", ".join(s["symbol"] for s in watching) if watching else "  (none)",
        nsw     = len(sw_sigs),
        sw      = "\n".join(sw_lines) or "  (none yet -- scan running)",
        nopen   = len(open_t),
        open_t  = "\n".join(open_lines) or "  (none)",
        pnl     = total_pnl,
        w       = wins,
        l       = losses,
        ver     = cfg.get("ai_version", 0),
        insight = cfg.get("ai_insight", "none"),
        focus   = cfg.get("ai_focus", "none"),
    )
    return ctx


@app.route("/chat")
def chat_page():
    has_key = bool(ANTHROPIC_KEY)
    return """<!DOCTYPE html><html><head>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0d1117;color:#e6edf3;font-family:-apple-system,Arial,sans-serif;
     font-size:13px;display:flex;flex-direction:column;height:100vh}}
.topbar{{background:#161b22;border-bottom:1px solid #30363d;padding:0 14px;
         display:flex;align-items:center;justify-content:space-between;
         height:48px;flex-shrink:0}}
.brand{{font-size:13px;font-weight:800;letter-spacing:1px;color:#e6edf3;
        text-transform:uppercase}}
.nav-link{{color:#58a6ff;text-decoration:none;font-size:11px;font-weight:500;margin-left:14px}}
.nav-link:hover{{text-decoration:underline}}
.nav-active{{color:#e6edf3;border-bottom:2px solid #58a6ff;padding-bottom:2px}}
#msgs{{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:12px}}
.msg-user{{align-self:flex-end;background:#1f6feb;color:#fff;border-radius:16px 16px 4px 16px;
           padding:10px 14px;max-width:82%;font-size:13px;line-height:1.5}}
.msg-ai{{align-self:flex-start;background:#161b22;border:1px solid #30363d;
         border-radius:4px 16px 16px 16px;padding:10px 14px;max-width:92%;
         font-size:13px;line-height:1.6;white-space:pre-wrap}}
.msg-ai strong{{color:#58a6ff}}
.msg-system{{align-self:center;color:#8b949e;font-size:11px;font-style:italic}}
.typing{{color:#8b949e;font-size:12px;font-style:italic;padding:4px 14px}}
.input-row{{background:#161b22;border-top:1px solid #30363d;padding:10px 12px;
            display:flex;gap:8px;flex-shrink:0}}
#inp{{flex:1;background:#0d1117;border:1px solid #30363d;border-radius:20px;
      color:#e6edf3;font-size:13px;padding:10px 16px;outline:none;
      font-family:-apple-system,Arial,sans-serif}}
#inp:focus{{border-color:#58a6ff}}
#send-btn{{background:#1f6feb;color:#fff;border:none;border-radius:20px;
           padding:10px 18px;font-size:13px;font-weight:700;cursor:pointer;
           white-space:nowrap}}
#send-btn:disabled{{background:#21262d;color:#8b949e;cursor:not-allowed}}
.chip{{display:inline-block;background:#21262d;color:#8b949e;font-size:11px;
       padding:4px 10px;border-radius:12px;cursor:pointer;margin:3px;
       border:1px solid #30363d}}
.chip:hover{{border-color:#58a6ff;color:#58a6ff}}
</style>
</head><body>

<div class='topbar'>
  <span class='brand'>Scanner Chat</span>
  <div>
    <a class='nav-link' href='/'>0DTE</a>
    <a class='nav-link' href='/swing'>Swing</a>
    <a class='nav-link nav-active' href='/chat'>Chat</a>
    <a class='nav-link' href='/ai'>AI</a>
    <a class='nav-link' href='/stats'>Stats</a>
  </div>
</div>

<div id='msgs'>
  <div class='msg-system'>Context is refreshed each message with live scanner data.</div>
  <div class='msg-ai'><strong>Scanner AI</strong>
I have full access to your live 0DTE signals, swing setups, open trades, P&L, and scanner config. Ask me anything:

<div style='margin-top:10px'>
<span class='chip' onclick='ask(this)'>What's the best setup right now?</span>
<span class='chip' onclick='ask(this)'>Explain the top 0DTE signal</span>
<span class='chip' onclick='ask(this)'>Any swing trades worth taking today?</span>
<span class='chip' onclick='ask(this)'>Should I be bullish or bearish today?</span>
<span class='chip' onclick='ask(this)'>What are my open positions?</span>
<span class='chip' onclick='ask(this)'>Compare the top 3 swing setups</span>
</div></div>
</div>

<div id='typing' class='typing' style='display:none'>Scanner AI is thinking...</div>

<div class='input-row'>
  <input id='inp' placeholder='{ph}' {dis} autocomplete='off'
         onkeydown='if(event.key==="Enter"&&!event.shiftKey){{event.preventDefault();send()}}'>
  <button id='send-btn' onclick='send()' {dis}>Send</button>
</div>

<script>
var history = [];

function ask(el) {{
  document.getElementById('inp').value = el.textContent;
  send();
}}

function addMsg(role, text) {{
  var div = document.createElement('div');
  div.className = role === 'user' ? 'msg-user' : 'msg-ai';
  if (role === 'assistant') {{
    div.innerHTML = '<strong>Scanner AI</strong>\\n' + escHtml(text);
  }} else {{
    div.textContent = text;
  }}
  document.getElementById('msgs').appendChild(div);
  div.scrollIntoView({{behavior:'smooth'}});
  return div;
}}

function escHtml(t) {{
  return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

function send() {{
  var inp = document.getElementById('inp');
  var txt = inp.value.trim();
  if (!txt) return;
  inp.value = '';
  addMsg('user', txt);
  history.push({{role:'user', content:txt}});

  var btn = document.getElementById('send-btn');
  btn.disabled = true;
  inp.disabled = true;
  document.getElementById('typing').style.display = 'block';

  fetch('/chat/send', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{history: history}})
  }})
  .then(r => r.json())
  .then(data => {{
    document.getElementById('typing').style.display = 'none';
    btn.disabled = false;
    inp.disabled = false;
    inp.focus();
    if (data.error) {{
      addMsg('assistant', 'Error: ' + data.error);
    }} else {{
      addMsg('assistant', data.reply);
      history.push({{role:'assistant', content:data.reply}});
      // Keep history to last 20 turns to avoid token overflow
      if (history.length > 40) history = history.slice(history.length - 40);
    }}
  }})
  .catch(err => {{
    document.getElementById('typing').style.display = 'none';
    btn.disabled = false;
    inp.disabled = false;
    addMsg('assistant', 'Network error: ' + err);
  }});
}}
</script>
</body></html>""".format(
        ph  = "Ask about signals, stocks, setups..." if has_key else "ANTHROPIC_API_KEY not set",
        dis = "" if has_key else "disabled",
    )


@app.route("/chat/send", methods=["POST"])
def chat_send():
    if not ANTHROPIC_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY not set"})

    try:
        data    = request.get_json(force=True)
        history = data.get("history", [])
        if not history:
            return jsonify({"error": "No message"})

        # Cap history at last 20 turns
        if len(history) > 20:
            history = history[-20:]

        # Build live context
        ctx = _build_scanner_context()

        system_prompt = """You are an expert trading assistant embedded inside a live 0DTE and swing options scanner. You have direct access to the scanner's current state shown below.

You help the trader:
- Interpret and rank the current signals
- Explain what the scanner is seeing and why
- Discuss individual stocks and option setups
- Give conviction ratings on specific trades
- Compare 0DTE vs swing opportunities
- Flag risks (counter-trend, low volume, extended moves)
- Answer general questions about options, technical analysis, and market structure

Be concise and direct -- this is a trading app on mobile. Use short paragraphs. Lead with the most actionable insight. Use dollar signs and percentages as shown in the data.

When discussing options: always mention the DTE, the delta, and whether the setup is 0DTE or swing (multi-day hold).

--- LIVE SCANNER DATA ---
{ctx}
--- END SCANNER DATA ---

You have full knowledge of this scanner's methodology:
- 0DTE signals use ORB breakouts, VWAP, confluence grading (A/B/C), volume, gap alignment, RS vs SPY, 1hr trend
- VWAP Trend signals fire when price stays consistently on one side of VWAP with trending structure (based on Zarattini & Aziz SSRN 2023 research)
- VWAP Mean Reversion signals fire when price touches VWAP deviation bands and reverses back toward the mean (best on range-bound days)
- IB Extension signals fire after 10:30 AM when price moves beyond the Initial Balance range (Market Profile methodology)
- VWAP Reclaim signals detect when price reclaims VWAP after being on the wrong side, with volume confirmation
- Swing signals use O'Neil Pivot Breakout, Wyckoff Spring, 52-Week Breakout, and Post-Earnings Continuation
- Fibonacci extensions (1.272 / 1.618 / 2.0) are price targets; 0.618 retrace is the stop level
- Swing options target delta ~0.55, 2-week expiry; 0DTE options target delta ~0.40""".format(ctx=ctx)

        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-20250514",
                "max_tokens": 1024,
                "system":     system_prompt,
                "messages":   history,
            },
            timeout=30,
        )

        if response.status_code != 200:
            return jsonify({"error": "API error {}".format(response.status_code)})

        reply = response.json()["content"][0]["text"]
        return jsonify({"reply": reply})

    except Exception as e:
        log("Chat error: {}".format(e))
        return jsonify({"error": str(e)})


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


@app.route("/options-test")
def options_test():
    """Test Alpaca options data for SPY - shows live ATM chain."""
    et        = pytz.timezone("America/New_York")
    today_str = datetime.now(et).strftime("%Y-%m-%d")
    spy_price = get_current_price("SPY") or 550.0
    lo = round(spy_price * 0.99, 2)
    hi = round(spy_price * 1.01, 2)
    results = {
        "keys_set":   bool(ALPACA_KEY and ALPACA_SECRET),
        "today":      today_str,
        "spy_price":  spy_price,
        "strike_range": "{} - {}".format(lo, hi),
    }
    try:
        headers = {
            "APCA-API-KEY-ID":     ALPACA_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET,
        }
        r = requests.get(
            "https://data.alpaca.markets/v1beta1/options/snapshots/SPY",
            headers=headers,
            params={"feed": "indicative", "expiration_date": today_str,
                    "type": "put", "strike_price_gte": lo,
                    "strike_price_lte": hi, "limit": 10},
            timeout=10)
        results["status"] = r.status_code
        if r.status_code == 200:
            snaps = r.json().get("snapshots", {})
            results["contracts_returned"] = len(snaps)
            sample = []
            for sym, s in list(snaps.items())[:5]:
                q = s.get("latestQuote") or {}
                g = s.get("greeks") or {}
                sample.append({
                    "symbol": sym,
                    "bid": q.get("bp"), "ask": q.get("ap"),
                    "delta": g.get("delta"),
                    "iv": s.get("impliedVolatility"),
                })
            results["sample"] = sample
        else:
            results["error"] = r.text[:300]
    except Exception as e:
        results["exception"] = str(e)
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
db_load_latest_config()   # restore AI config from last session
threading.Thread(target=background_scheduler, daemon=True).start()
threading.Thread(target=telegram_poller,      daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
