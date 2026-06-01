from flask import Flask, jsonify, render_template_string, request, redirect
import requests
import os
import statistics
import math
import threading
import time
import json
import sqlite3
import db_utils
from datetime import datetime, timedelta
import pytz

# =============================================
# QUANT EDGE MODULES (new)
# =============================================
# These are optional — wrapped in try/except so the app still boots
# if a module file is missing or its provider keys aren't set.
try:
    import volume_truth
    HAS_VOLUME_TRUTH = True
except ImportError as _e:
    HAS_VOLUME_TRUTH = False
    print("[init] volume_truth unavailable: {}".format(_e))

try:
    import safety_gates
    HAS_SAFETY_GATES = True
except ImportError as _e:
    HAS_SAFETY_GATES = False
    print("[init] safety_gates unavailable: {}".format(_e))

try:
    import regime_filter
    HAS_REGIME = True
except ImportError as _e:
    HAS_REGIME = False
    print("[init] regime_filter unavailable: {}".format(_e))

try:
    import overnight_context
    HAS_OVERNIGHT = True
except ImportError as _e:
    HAS_OVERNIGHT = False
    print("[init] overnight_context unavailable: {}".format(_e))

try:
    import gamma_exposure
    HAS_GEX = True
except ImportError as _e:
    HAS_GEX = False
    print("[init] gamma_exposure unavailable: {}".format(_e))

try:
    import new_strategies
    HAS_NEW_STRATS = True
except ImportError as _e:
    HAS_NEW_STRATS = False
    print("[init] new_strategies unavailable: {}".format(_e))

try:
    import iv_rank
    HAS_IV_RANK = True
except ImportError as _e:
    HAS_IV_RANK = False
    print("[init] iv_rank unavailable: {}".format(_e))

try:
    import oi_delta
    HAS_OI_DELTA = True
except ImportError as _e:
    HAS_OI_DELTA = False
    print("[init] oi_delta unavailable: {}".format(_e))

try:
    import market_profile
    HAS_MPROFILE = True
except ImportError as _e:
    HAS_MPROFILE = False
    print("[init] market_profile unavailable: {}".format(_e))

try:
    import options_flow
    HAS_OPT_FLOW = True
except ImportError as _e:
    HAS_OPT_FLOW = False
    print("[init] options_flow unavailable: {}".format(_e))

try:
    import targets as targets_mod
    HAS_TARGETS = True
except ImportError as _e:
    HAS_TARGETS = False
    print("[init] targets unavailable: {}".format(_e))

try:
    import key_levels as key_levels_mod
    HAS_KEY_LEVELS = True
except ImportError as _e:
    HAS_KEY_LEVELS = False
    print("[init] key_levels unavailable: {}".format(_e))

try:
    import scanner_core
    HAS_SCANNER_CORE = True
except ImportError as _e:
    HAS_SCANNER_CORE = False
    print("[init] scanner_core unavailable: {}".format(_e))

try:
    import vol_oi as vol_oi_mod
    HAS_VOL_OI = True
except ImportError as _e:
    HAS_VOL_OI = False
    print("[init] vol_oi unavailable: {}".format(_e))

# Operating mode: 'premarket' = single morning brief only (cheap, ~$2/mo)
#                 'continuous' = refresh regime/GEX throughout the day (~$80/mo)
# Default is premarket since most users only need a morning brief.
OPERATING_MODE = os.getenv("OPERATING_MODE", "premarket").strip().lower()
if OPERATING_MODE not in ("premarket", "continuous"):
    OPERATING_MODE = "premarket"
print("[init] Operating mode: {}".format(OPERATING_MODE))

# Shared module-level state populated daily by the scheduler
_market_state = {
    "regime":           None,   # output of regime_filter.classify_regime()
    "premarket_brief":  None,   # output of overnight_context.get_premarket_brief()
    "gex_bias":         None,   # output of gamma_exposure.get_gex_bias()
    "regime_ts":        0,      # epoch when regime was last refreshed
}
_market_state_lock = threading.Lock()


# =============================================
# APP SETUP
# =============================================

app = Flask(__name__)

SCAN_INTERVAL = 300
ORB_BARS      = 6       # 30 min ORB (6 x 5min bars) - institutional standard

# =============================================
# PRODUCT TIERING (see scanner redesign)
#   INDEX (SPX/NDX)  -> context only, no signal cards (no cash-index options)
#   ETF   (SPY/QQQ)  -> BOTH weekly and intraday/0DTE
#   STOCK (~70 names) -> weekly only
# =============================================
ETF_PRODUCTS   = ["SPY", "QQQ"]            # weekly + intraday/0DTE
INDEX_PRODUCTS = ["SPX", "NDX"]            # context only (display level + RS)

# Intraday / 0DTE tradeable universe. ETFs carry daily 0DTE options; the
# liquid single-stock momentum names below trade the same ORB / VWAP / pullback
# logic but resolve to the nearest weekly (Fri) contract via get_liquid_option.
# Kept to a focused, high-liquidity set so the 5-min sweep stays cheap and the
# IEX volume ratios stay meaningful. These are exactly the names that trend
# cleanly intraday -- the case where the engine previously had nothing to show.
INTRADAY_STOCKS = [
    "NVDA", "TSLA", "AMD", "AAPL", "META", "AMZN", "MSFT", "GOOGL",
    "NFLX", "AVGO", "MU", "PLTR", "COIN", "SMCI",
]
INTRADAY_SYMBOLS = ETF_PRODUCTS + INTRADAY_STOCKS

# Back-compat alias for the several aux paths (earnings prefetch, digests) that
# iterate SYMBOLS + SWING_SYMBOLS over the full coverage set.
SYMBOLS = list(ETF_PRODUCTS)

# Broader universe for the WEEKLY scanner - liquid, optionable stocks.
# SPY/QQQ are intentionally NOT here (they are ETF_PRODUCTS); leveraged ETFs
# (TQQQ/SOXL) are excluded from weekly due to decay.
SWING_UNIVERSE = [
    # Mega-cap tech
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD", "AVGO", "CRM",
    # Semis & hardware
    "INTC", "MU", "QCOM", "TXN", "AMAT", "LRCX", "KLAC", "MRVL", "ON", "SMCI",
    # AI infrastructure (datacenter compute, power, cooling, networking)
    "CRWV", "GEV", "VRT", "ANET",
    # Finance
    "JPM", "GS", "MS", "BAC", "C", "V", "MA", "AXP", "BX", "KKR",
    # Healthcare & biotech
    "LLY", "UNH", "ABBV", "MRK", "PFE", "JNJ", "GILD", "REGN", "BIIB", "MRNA",
    # Energy & industrials
    "XOM", "CVX", "OXY", "SLB", "CAT", "DE", "HON", "RTX", "LMT", "GE",
    # Consumer & retail
    "COST", "WMT", "HD", "NKE", "SBUX", "MCD", "TGT", "LULU", "DECK", "RH",
    # Non-index ETFs with strong options
    "IWM", "XLK", "XLF", "GLD", "SLV", "ARKK",
]

# The weekly tier scans the ETF products PLUS the stock universe.
# Alias used by run_swing_scan  [FIX 1: confirmed -- ensures symbol list is found]
SWING_SYMBOLS = ETF_PRODUCTS + SWING_UNIVERSE

ALPACA_KEY    = os.getenv("APCA_API_KEY_ID", "").strip()
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY", "").strip()

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET
}

DATA_URL  = "https://data.alpaca.markets/v2/stocks/{}/bars"
QUOTE_URL = "https://data.alpaca.markets/v2/stocks/{}/quotes/latest"
CLOCK_URL = "https://paper-api.alpaca.markets/v2/clock"
CALENDAR_URL = "https://paper-api.alpaca.markets/v2/calendar"

# Auto-detect persistent storage: Railway volume > DATA_DIR env > /tmp fallback
_data_dir = os.getenv("DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "/tmp"
ALERT_FILE     = _data_dir + "/last_alert.json"
DB_FILE        = _data_dir + "/trades.db"
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "").strip()

state_lock   = threading.Lock()
debug_log    = []
all_signals  = []
next_scan_at = 0
bot_enabled  = True

swing_signals    = []
next_swing_scan  = 0
all_swing_signals  = []
swing_next_scan_at = 0
swing_lock         = threading.Lock()
SWING_SCAN_INTERVAL = 900   # 15 min between swing scans
# Weekly alert dedup: fire one telegram alert per (symbol, direction,
# week_expiry) so a setup alerts once for the week's settlement and re-arms
# automatically when the expiry rolls to next Friday. In-memory is fine -- a
# container restart simply re-alerts the still-active setups once.
_weekly_alerted     = set()
_weekly_alert_lock  = threading.Lock()


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
    # Hard cutoff for emitting new 0DTE SIGNAL rows. After this ET hour
    # the picker still runs but the row is demoted to WATCHING since
    # time decay dominates and stops are unlikely to clear before
    # close. Set to 16.0 to disable the cutoff entirely.
    "zero_dte_cutoff_hour": 14.5,

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
    # Trade WITH the tape: counter-trend setups are suppressed at detection,
    # and only A/B-grade signals are alerted (C-grade is near-random
    # confluence -- still scanned/shown on the dashboard, just not alerted).
    "counter_trend_allowed":    False,
    "min_grade":                "C",      # dashboard floor (D is suppressed)
    "alert_min_grade":          "B",      # only A/B grades fire alerts
    # Anti-chase: skip an intraday breakout once price has already run more
    # than this fraction of the ORB range past the trigger -- entering
    # extended is the dominant intraday loss source. Demotes to WATCHING.
    "max_breakout_extension":   0.6,
    # Trend-day pullback re-entry. On a clean trend day every breakout is
    # "extended" by the time the 5-min scan sees it, so anti-chase suppresses
    # 100% of signals -- the engine goes dark exactly when the trend is best.
    # Instead of only demoting, look for a continuation entry: price ran
    # extended (trend confirmed), pulled back toward the trigger but held it as
    # support (higher low), and is now resuming on volume. This is the high-
    # probability re-entry, with a tight stop under the held trigger.
    "pullback_reentry_enabled": True,
    "pullback_min_depth":       0.25,   # min dip off the swing, in ORB ranges
    "pullback_vol_min":         1.1,    # resumption time-adjusted volume floor
    # ORB underlying stop = this multiple of the ORB range beyond entry. 1.0x
    # (a full range, ~1:1 vs the 1.0x T1) gives breakouts room to retest the
    # trigger without stopping out on noise; raised from the old 0.5x.
    "orb_stop_mult":            1.0,
    # Weekly long setups require the broad tape to agree: positive RS vs SPY
    # and SPY itself in an uptrend (above its 50DMA). Avoids firing CALL-only
    # weeklies into a falling market.
    "weekly_require_uptrend":   True,
    "weekly_min_rs":            0.0,
    "weekly_max_breakout_age":  3,        # skip breakouts older than N sessions

    # --- Clear-air (resistance/support proximity) cap ---
    # Levels within this % of current price are treated as already tested /
    # within noise and do NOT block the path to T1. Without this, an intraday
    # 1H swing-high sitting fractions of a percent overhead caps nearly every
    # signal at C. A weak (1H) blocking level applies a softer one-grade
    # penalty rather than the hard C cap reserved for 4hr/daily levels.
    "clear_air_tol_pct":        0.12,
    "clear_air_weak_strength":  1,

    # --- AI state ---
    "ai_insight": "Baseline config -- collecting trade data to begin optimization.",
    "ai_focus":   "Alert only trend-aligned A/B grade signals; log every scanned setup to build the dataset.",
    "ai_version": 0,
    # ISO-UTC cutoff. Trades logged before this are excluded from AI
    # tuning (set whenever the config is reset to baseline).
    "learning_epoch": "",
}

# Hard code-level floor for AI training data. The learning_epoch lives in the
# DB config on the persistent volume and survives redeploys, so a manual reset
# isn't guaranteed to stick across a deploy. This floor is enforced in addition
# to learning_epoch (the later of the two wins), guaranteeing the loop never
# trains on pre-redesign trades whose entry logic no longer exists. Bump this
# when the signal logic changes materially.
AI_TRAINING_FLOOR = "2026-05-29T00:00:00+00:00"

# Minimum post-epoch closed trades before the AI may tune anything.
AI_MIN_TOTAL_SAMPLES = 30

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


def log_warn(msg):
    # Stays on stdout (the platform classifies stdout as `info`) but the
    # WARN prefix makes it filterable in log search.
    log("WARN: {}".format(msg))


_state_log_last = {}
_state_log_lock = threading.Lock()


def log_state_transition(key, state, msg, level="warn"):
    """
    Emit `msg` only when `state` changes for `key`. Prevents per-scan
    spam for steady-state conditions (e.g. VIX None every 5 min) while
    still logging the moment it becomes None and the moment it recovers.
    """
    with _state_log_lock:
        prev = _state_log_last.get(key)
        if prev == state:
            return
        _state_log_last[key] = state
    if level == "warn":
        log_warn(msg)
    elif level == "error":
        log_error(msg)
    else:
        log(msg)


def log_error(msg):
    # Writes to stderr so the platform log shipper tags it as `error`.
    import sys as _sys
    ts    = datetime.now(pytz.utc).strftime("%H:%M:%S")
    entry = "[{}] ERROR: {}".format(ts, msg)
    print(entry, file=_sys.stderr, flush=True)
    with state_lock:
        debug_log.append(entry)
        if len(debug_log) > 150:
            debug_log.pop(0)


LOG_JSON = os.getenv("LOG_JSON", "").strip() in ("1", "true", "yes")


def log_event(event, level="info", **fields):
    """Structured log entry. Emits JSON when LOG_JSON=1 (so the log shipper
    can index `symbol`/`source`/`status` etc. as fields), otherwise a
    readable `[HH:MM:SS] event | k=v k=v` line. Level controls severity:
    `error` writes to stderr (which the platform tags as error), `info`
    and `warn` write to stdout."""
    import sys as _sys
    ts = datetime.now(pytz.utc).strftime("%H:%M:%S")
    if LOG_JSON:
        payload = {"ts": ts, "level": level, "event": event}
        for k, v in fields.items():
            payload[k] = v
        line = json.dumps(payload, default=str)
    else:
        kvs = " ".join("{}={}".format(k, v) for k, v in fields.items())
        prefix = {"error": "ERROR: ", "warn": "WARN: "}.get(level, "")
        line = "[{}] {}{}{}".format(ts, prefix, event,
                                     " | " + kvs if kvs else "")
    stream = _sys.stderr if level == "error" else _sys.stdout
    print(line, file=stream, flush=True)
    with state_lock:
        debug_log.append(line)
        if len(debug_log) > 150:
            debug_log.pop(0)


def _databento_blocked():
    """True when the Databento billing breaker is currently engaged."""
    try:
        import databento_adapter as _da
        return bool(_da.billing_status().get("blocked"))
    except Exception:
        return False


_last_auth_state = None


def _log_auth_state_if_changed():
    """Log auth booleans only on first call and when any flag flips.
    Avoids the per-scan "Key set: True | Secret set: True | Bot: True"
    boilerplate that dominated the log volume."""
    global _last_auth_state
    state = (bool(ALPACA_KEY), bool(ALPACA_SECRET), bool(bot_enabled))
    if state == _last_auth_state:
        return
    msg = "Auth: Alpaca key={} secret={} | Telegram bot={}".format(*state)
    if _last_auth_state is None:
        log(msg)
    else:
        # A flip on a previously-set credential is worth surfacing.
        log_warn(msg + " (changed)")
    _last_auth_state = state


# Werkzeug's default request handler writes every "GET / 200" line to stderr,
# which the log shipper then tags as `error`. Silence the 2xx/3xx access logs
# at the source so only actual server errors (5xx) bubble up there.
#
# yfinance's logger emits "$SYM: possibly delisted" + "Failed to get ticker"
# whenever Yahoo returns an empty body (rate-limit), which is misleading
# (SPY/NVDA/etc. are obviously not delisted) and noisy. We quiet it and let
# the Alpaca/Databento fallback paths handle the real outcome.
try:
    import logging as _logging
    _logging.getLogger("werkzeug").setLevel(_logging.WARNING)
    _logging.getLogger("yfinance").setLevel(_logging.CRITICAL)
except Exception:
    pass


# =============================================
# DATABASE
# =============================================

def init_db():
    conn = db_utils.connect(DB_FILE)
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
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT,
            symbol       TEXT,
            direction    TEXT,
            premium      REAL,
            contracts    INTEGER,
            stop         REAL,
            target       REAL,
            outcome      TEXT,
            exit_price   REAL,
            pnl          REAL,
            r_mult       REAL,
            grade        TEXT,
            grade_pts    INTEGER,
            gap_pct      REAL,
            gap_dir      TEXT,
            rs           REAL,
            entry_hour   REAL,
            entry_under  REAL,
            signal_type  TEXT
        )
    """)
    # Migrate existing trades table
    for col, coltype in [("grade","TEXT"), ("grade_pts","INTEGER"),
                          ("gap_pct","REAL"), ("gap_dir","TEXT"),
                          ("rs","REAL"), ("entry_hour","REAL"),
                          ("entry_under","REAL"), ("signal_type","TEXT"),
                          # Auto position-tracking: underlying targets the live
                          # monitor resolves against, the tier (horizon) the
                          # position occupies, and how it was opened.
                          ("und_stop","REAL"), ("und_target_t1","REAL"),
                          ("und_target_t2","REAL"), ("horizon","TEXT"),
                          ("mode","TEXT")]:
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN {} {}".format(col, coltype))
        except:
            pass

    # Migrate signals table. The original CREATE only declared a handful of
    # columns, but db_log_signal inserts several more (entry_under, und_stop,
    # und_target_t1/t2, signal_type, grade, grade_pts) -- without these the
    # insert silently failed. Also add the unified-scanner redesign columns
    # (horizon, tier, product_class, conviction, rationale_json).
    for col, coltype in [("entry_under","REAL"), ("und_stop","REAL"),
                          ("und_target_t1","REAL"), ("und_target_t2","REAL"),
                          ("signal_type","TEXT"), ("grade","TEXT"),
                          ("grade_pts","INTEGER"), ("horizon","TEXT"),
                          ("tier","INTEGER"), ("product_class","TEXT"),
                          ("conviction","REAL"), ("rationale_json","TEXT")]:
        try:
            conn.execute("ALTER TABLE signals ADD COLUMN {} {}".format(col, coltype))
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
        conn = db_utils.connect(DB_FILE)
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
        conn = db_utils.connect(DB_FILE)
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


def reset_ai_config_to_baseline(reason="manual"):
    """
    Wipe all AI tuning: restore DEFAULT_CONFIG and stamp a fresh
    learning_epoch so the AI only learns from trades logged AFTER this
    point. Persisted as the newest ai_config row so it survives restarts.
    """
    baseline = dict(DEFAULT_CONFIG)
    baseline["learning_epoch"] = datetime.now(pytz.utc).isoformat()
    baseline["ai_insight"] = ("Config reset to baseline ({}). Collecting "
                              "fresh post-reset trades.".format(reason))
    baseline["ai_focus"]   = ("Trade all A/B/C signals and log outcomes; "
                              "AI tuning resumes once data accumulates.")
    update_config(baseline, updated_by="reset_baseline")
    db_save_ai_config(get_config(), trigger="reset_{}".format(reason))
    # Verify the epoch actually persisted. If the DB write silently
    # failed, idempotency is broken and every restart would re-wipe
    # accumulated post-epoch trades -- surface that loudly.
    try:
        conn = db_utils.connect(DB_FILE)
        row = conn.execute(
            "SELECT config_json FROM ai_config ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        persisted = json.loads(row[0]).get("learning_epoch") if row else None
        if persisted != baseline["learning_epoch"]:
            log_event("ai.reset_not_persisted", level="error",
                      reason=reason, expected=baseline["learning_epoch"],
                      persisted=persisted)
    except Exception as e:
        log_event("ai.reset_verify_failed", level="error", error=str(e))
    log("AI: config reset to baseline ({}); learning_epoch={}".format(
        reason, baseline["learning_epoch"]))


def _maybe_reset_ai_baseline():
    """
    Idempotent: if the restored config has no learning_epoch it predates
    the reset, so baseline it exactly once. After that this is a no-op.
    """
    if not get_config().get("learning_epoch"):
        log("AI: pre-reset config detected -- applying one-time baseline reset")
        reset_ai_config_to_baseline(reason="auto_migration")


def db_load_latest_config():
    """Load most recent AI config from DB into memory on startup."""
    try:
        conn = db_utils.connect(DB_FILE)
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
        conn = db_utils.connect(DB_FILE)
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
        conn = db_utils.connect(DB_FILE)
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
        conn = db_utils.connect(DB_FILE)
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
        conn = db_utils.connect(DB_FILE)
        c    = conn.cursor()
        c.execute("UPDATE ai_proposals SET status='dismissed', dismissed_at=? WHERE id=?",
                  (datetime.now(pytz.utc).isoformat(), proposal_id))
        conn.commit()
        conn.close()
    except Exception as e:
        log("DB dismiss proposal error: {}".format(e))


def db_get_all_closed_trades():
    """
    Closed trades for AI analysis -- ALL modes (paper + real), since the
    AI must learn from every signal it suggested. P&L is intentionally
    NOT selected: tuning is on pure win/loss + R-multiple (were the price
    targets hit before the stop), never on dollars. Trades logged before
    the active learning_epoch are excluded -- pre-reset data lacks
    trustworthy VIX/GEX context and must not influence tuning.
    """
    try:
        # The later of the configured learning_epoch and the hard code-level
        # training floor wins -- so a stale DB epoch can never reintroduce
        # pre-floor trades after a redeploy.
        epoch = max(get_config().get("learning_epoch") or "", AI_TRAINING_FLOOR)
        conn = db_utils.connect(DB_FILE)
        c    = conn.cursor()
        c.execute("""
            SELECT symbol, direction, outcome, r_mult,
                   grade, grade_pts, gap_pct, gap_dir, rs, entry_hour, ts,
                   signal_type
            FROM trades
            WHERE outcome != 'OPEN' AND ts >= ?
            ORDER BY ts DESC
        """, (epoch,))
        rows = c.fetchall()
        conn.close()
        cols = ["symbol","direction","outcome","r_mult",
                "grade","grade_pts","gap_pct","gap_dir","rs","entry_hour","ts",
                "signal_type"]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        log("DB get all closed trades error: {}".format(e))
        return []


def db_log_signal(sig):
    try:
        # Pick the underlying stop / targets the scanner already computed
        # (per-direction keys: und_call_*, und_put_*). Paper trader walks
        # these against the rest of the day's 5-min bars at EOD.
        direction = sig.get("direction")
        if direction == "CALL":
            und_stop = sig.get("und_call_stop")
            und_t1   = sig.get("und_call_t1")
            und_t2   = sig.get("und_call_t2")
        elif direction == "PUT":
            und_stop = sig.get("und_put_stop")
            und_t1   = sig.get("und_put_t1")
            und_t2   = sig.get("und_put_t2")
        else:
            und_stop = und_t1 = und_t2 = None

        rationale = sig.get("rationale")
        rationale_json = None
        if rationale is not None:
            try:
                rationale_json = json.dumps(rationale)
            except Exception:
                rationale_json = None

        conn = db_utils.connect(DB_FILE)
        c    = conn.cursor()
        c.execute("""
            INSERT INTO signals
            (ts, symbol, direction, price, score, premium, strike, contracts,
             stop, target,
             entry_under, und_stop, und_target_t1, und_target_t2,
             signal_type, grade, grade_pts,
             horizon, tier, product_class, conviction, rationale_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now(pytz.utc).isoformat(),
            sig.get("symbol"), direction,
            sig.get("price"),  sig.get("score"),
            sig.get("premium"), str(sig.get("strike","")),
            None,               # contracts: position size no longer modeled
            sig.get("stop"), sig.get("target"),
            sig.get("price"),   # entry_under = underlying price at signal time
            und_stop, und_t1, und_t2,
            sig.get("signal_type"), sig.get("grade"), sig.get("grade_pts"),
            sig.get("horizon"), sig.get("tier"), sig.get("product_class"),
            sig.get("conviction"), rationale_json,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        log("DB signal log error: {}".format(e))


def db_log_trade(symbol, direction, premium, contracts=None, stop=None, target=None,
                  grade=None, grade_pts=None, gap_pct=None,
                  gap_dir=None, rs=None, entry_hour=None,
                  entry_under=None, signal_type=None,
                  und_stop=None, und_target_t1=None, und_target_t2=None,
                  horizon=None, mode=None):
    try:
        conn = db_utils.connect(DB_FILE)
        c    = conn.cursor()
        et   = pytz.timezone("America/New_York")
        if entry_hour is None:
            now        = datetime.now(et)
            entry_hour = round(now.hour + now.minute / 60.0, 2)
        c.execute("""
            INSERT INTO trades
            (ts,symbol,direction,premium,contracts,stop,target,outcome,
             grade,grade_pts,gap_pct,gap_dir,rs,entry_hour,
             entry_under,signal_type,
             und_stop,und_target_t1,und_target_t2,horizon,mode)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now(pytz.utc).isoformat(),
            symbol, direction, premium, contracts, stop, target, "OPEN",
            grade, grade_pts, gap_pct, gap_dir, rs, entry_hour,
            entry_under, signal_type,
            und_stop, und_target_t1, und_target_t2, horizon, mode
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
        conn = db_utils.connect(DB_FILE)
        c    = conn.cursor()
        c.execute("SELECT premium, contracts FROM trades WHERE id=?", (trade_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return
        premium, contracts = row
        # Per-contract P&L. Position size is no longer part of the model, so
        # P&L is always expressed on a single-contract basis (contracts may be
        # NULL on new rows). r_mult is already size-independent. Both are NULL
        # when we have no entry premium (e.g. an auto position with no live
        # option) -- the outcome (WIN/LOSS) still stands on underlying targets.
        if premium and exit_price is not None:
            pnl    = round((exit_price - premium) * 100, 2)
            r_mult = round((exit_price - premium) / (premium * 0.45), 2)
        else:
            pnl = r_mult = None
        c.execute("""
            UPDATE trades SET outcome=?, exit_price=?, pnl=?, r_mult=?
            WHERE id=?
        """, (outcome, exit_price, pnl, r_mult, trade_id))
        conn.commit()
        conn.close()
        log("Trade {} closed: {} pnl={}".format(trade_id, outcome, pnl))
    except Exception as e:
        log("DB close trade error: {}".format(e))


def db_get_today_trades():
    try:
        et    = pytz.timezone("America/New_York")
        today = datetime.now(et).strftime("%Y-%m-%d")
        conn  = db_utils.connect(DB_FILE)
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
        conn = db_utils.connect(DB_FILE)
        c    = conn.cursor()
        c.execute("""
            SELECT id,symbol,direction,premium,contracts,stop,target,ts,
                   entry_under,signal_type,grade,grade_pts
            FROM trades WHERE outcome='OPEN'
            ORDER BY ts DESC
        """)
        rows = c.fetchall()
        conn.close()
        cols = ["id","symbol","direction","premium","contracts","stop","target","ts",
                "entry_under","signal_type","grade","grade_pts"]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        log("DB open trades error: {}".format(e))
        return []


# =============================================
# ACTIVE POSITION GATE  (one live trade per tier; auto-tracked on alert)
# =============================================
#
# When a setup is alerted it auto-opens a tracked position (mode='auto') that
# occupies its tier (WEEKLY or INTRADAY). While a tier holds a live position
# the scanner emits no new alerts in that tier -- the two tiers are independent,
# so one weekly and one intraday position can be live at once. The live monitor
# resolves auto positions against the UNDERLYING targets (T1 = win, stop = loss)
# each scan, then frees the tier. Manually-taken trades (mode != 'auto') also
# occupy a tier so they mute alerts, but are only closed by the user.

_OPEN_POS_COLS = ["id", "symbol", "direction", "premium", "stop", "target",
                  "ts", "entry_under", "signal_type", "und_stop",
                  "und_target_t1", "und_target_t2", "horizon", "mode"]


def db_get_open_positions(horizon=None):
    """Open trades (optionally filtered to a tier) with underlying-target cols."""
    try:
        conn = db_utils.connect(DB_FILE)
        c    = conn.cursor()
        sql = ("SELECT id,symbol,direction,premium,stop,target,ts,entry_under,"
               "signal_type,und_stop,und_target_t1,und_target_t2,horizon,mode "
               "FROM trades WHERE outcome='OPEN'")
        params = ()
        if horizon:
            sql += " AND horizon=?"
            params = (horizon,)
        sql += " ORDER BY ts DESC"
        c.execute(sql, params)
        rows = c.fetchall()
        conn.close()
        return [dict(zip(_OPEN_POS_COLS, r)) for r in rows]
    except Exception as e:
        log("DB open positions error: {}".format(e))
        return []


def tier_has_open_position(horizon):
    """True if the given tier (WEEKLY/INTRADAY) already holds a live position."""
    return bool(db_get_open_positions(horizon))


def open_auto_position(sig):
    """Auto-open a tracked position for an alerted signal. Returns trade_id.

    Stores the option premium stop/target (size-free, per-contract) and the
    UNDERLYING stop/T1/T2 the live monitor resolves against.
    """
    direction = sig.get("direction")
    if direction == "CALL":
        und_stop = sig.get("und_call_stop")
        und_t1   = sig.get("und_call_t1")
        und_t2   = sig.get("und_call_t2")
    elif direction == "PUT":
        und_stop = sig.get("und_put_stop")
        und_t1   = sig.get("und_put_t1")
        und_t2   = sig.get("und_put_t2")
    else:
        und_stop = und_t1 = und_t2 = None
    # Weekly signals carry direction-agnostic t1/t2/stop too -- fall back to them.
    und_stop = und_stop if und_stop is not None else sig.get("stop")
    und_t1   = und_t1   if und_t1   is not None else sig.get("t1")
    und_t2   = und_t2   if und_t2   is not None else sig.get("t2")

    return db_log_trade(
        sig.get("symbol"), direction, sig.get("premium"),
        stop=sig.get("stop"), target=sig.get("target"),
        grade=sig.get("grade"), grade_pts=sig.get("grade_pts"),
        gap_pct=sig.get("gap_pct"), gap_dir=sig.get("gap_dir"),
        rs=sig.get("rs", sig.get("spy_rs")),
        entry_under=sig.get("price"), signal_type=sig.get("signal_type"),
        und_stop=und_stop, und_target_t1=und_t1, und_target_t2=und_t2,
        horizon=sig.get("horizon", "INTRADAY"), mode="auto")


def _underlying_extent(pos):
    """High/low of the underlying since entry, for touch detection.

    INTRADAY: today's 5-min bars at/after the entry timestamp.
    WEEKLY:   daily bars since the entry date, plus today's intraday range.
    Falls back to the current price when bars are unavailable.
    """
    symbol = pos["symbol"]
    hi = lo = None
    try:
        entry_ts = pos.get("ts") or ""
        if pos.get("horizon") == "WEEKLY":
            daily = get_daily(symbol) or []
            entry_day = entry_ts[:10]
            rel = [b for b in daily if (b.get("t") or "")[:10] >= entry_day]
            for b in rel:
                hi = b["h"] if hi is None else max(hi, b["h"])
                lo = b["l"] if lo is None else min(lo, b["l"])
            intra = get_intraday(symbol) or []
            for b in intra:
                hi = b["h"] if hi is None else max(hi, b["h"])
                lo = b["l"] if lo is None else min(lo, b["l"])
        else:
            intra = get_intraday(symbol) or []
            rel = [b for b in intra if (b.get("t") or "") >= entry_ts] or intra
            for b in rel:
                hi = b["h"] if hi is None else max(hi, b["h"])
                lo = b["l"] if lo is None else min(lo, b["l"])
    except Exception:
        pass
    if hi is None or lo is None:
        px = get_current_price(symbol)
        if px:
            hi = lo = px
    return hi, lo


def monitor_active_positions():
    """Resolve auto-tracked positions against underlying targets each scan.

    T1 touched -> WIN, stop touched -> LOSS (stop-first if both in the same
    window, conservatively). Weekly positions past their Friday expiry are
    force-closed so the tier frees up. Only mode='auto' rows are auto-resolved;
    manual trades are left for the user to close.
    """
    et = pytz.timezone("America/New_York")
    today = datetime.now(et).strftime("%Y-%m-%d")
    for pos in db_get_open_positions():
        if pos.get("mode") != "auto":
            continue
        direction = pos.get("direction")
        und_stop = pos.get("und_stop")
        und_t1   = pos.get("und_target_t1")
        premium  = pos.get("premium")
        # Option exit-premium proxies (our fixed stop/target levels), used so
        # P&L stays consistent for auto positions we don't price live.
        opt_target = pos.get("target") or (round(premium * 1.4, 2) if premium else None)
        opt_stop   = pos.get("stop")   or (round(premium * 0.55, 2) if premium else None)

        hi, lo = _underlying_extent(pos)

        outcome = exit_price = None
        if hi is not None and lo is not None and und_stop is not None:
            if direction == "CALL":
                stop_hit   = lo <= und_stop
                target_hit = und_t1 is not None and hi >= und_t1
            else:
                stop_hit   = hi >= und_stop
                target_hit = und_t1 is not None and lo <= und_t1
            if stop_hit:                       # stop-first if both touched
                outcome, exit_price = "LOSS", opt_stop
            elif target_hit:
                outcome, exit_price = "WIN", opt_target

        # Weekly expiry reached without resolution -> force close to free tier.
        if outcome is None and pos.get("horizon") == "WEEKLY":
            wk = (pos.get("signal_type") or "")
            try:
                _exp, _dte = current_week_expiry(
                    zero_dte_cutoff=get_config().get("zero_dte_cutoff_hour", 14.5))
            except Exception:
                _exp = None
            # If the entry was for a prior week's settlement, it has expired.
            entry_day = (pos.get("ts") or "")[:10]
            if entry_day and entry_day < today:
                px = get_current_price(pos["symbol"])
                eu = pos.get("entry_under")
                if px and eu:
                    favorable = (px >= eu) if direction == "CALL" else (px <= eu)
                    # Only force-close a multi-day weekly the day it expires; a
                    # mid-week unresolved position keeps riding.
                    if _exp and today >= _exp:
                        outcome = "WIN" if favorable else "LOSS"
                        exit_price = opt_target if favorable else opt_stop

        if outcome:
            db_close_trade(pos["id"], exit_price, outcome)
            log("Auto position {} {} {} resolved {} (underlying targets)".format(
                pos["id"], pos["symbol"], direction, outcome))


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
        if resp.status_code == 200:
            try:
                mid = resp.json().get("result", {}).get("message_id")
            except Exception:
                mid = None
            log_event("telegram.send", status=200, message_id=mid)
            return True
        log_event("telegram.error", level="error",
                  status=resp.status_code, body=resp.text[:150])
        return False
    except Exception as e:
        log_event("telegram.exception", level="error", error=str(e))
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

_last_clock_open = None


_trading_day_cache = {"date": None, "val": None}


def is_trading_day():
    """
    True if today (ET) is a US equity trading session — holiday-aware.

    Time-of-day independent: still True on a session day after the close,
    so EOD jobs (GEX/OI/IV/AI) fire on Friday afternoon but NOT on the
    weekend or on market holidays. Source is Alpaca's calendar endpoint;
    falls back to a weekday check only if the API is unreachable.
    """
    et    = pytz.timezone("America/New_York")
    today = datetime.now(et).strftime("%Y-%m-%d")
    if _trading_day_cache["date"] == today:
        return _trading_day_cache["val"]

    val = None
    try:
        r = requests.get(CALENDAR_URL, headers=HEADERS,
                         params={"start": today, "end": today}, timeout=5)
        if r.status_code == 200:
            days = r.json()
            # Non-empty array => Alpaca lists a session for today.
            val = bool(isinstance(days, list) and len(days) > 0)
    except Exception as e:
        log_event("calendar.error", level="warn", error=str(e))

    if val is None:  # API failure: weekday heuristic (misses holidays)
        val = datetime.now(et).weekday() < 5

    _trading_day_cache["date"] = today
    _trading_day_cache["val"]  = val
    return val


def market_open():
    global _last_clock_open
    try:
        r = requests.get(CLOCK_URL, headers=HEADERS, timeout=5)
        if r.status_code == 200:
            clock  = r.json()
            is_open = clock.get("is_open", False)
            # Only log the clock on an open<->closed transition; the
            # per-scan "Clock HTTP 200 / Clock: {…}" pair was ~165 lines
            # per closed session.
            if is_open != _last_clock_open:
                log_event("market.clock", is_open=is_open,
                          next_open=clock.get("next_open"),
                          next_close=clock.get("next_close"))
                _last_clock_open = is_open
            return is_open
        log_event("market.clock_error", level="warn",
                  status=r.status_code, body=r.text[:100])
    except Exception as e:
        log_event("market.clock_exception", level="warn", error=str(e))
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

import data_fetcher


def get_intraday(symbol):
    return data_fetcher.get_intraday(symbol)


def get_daily(symbol):
    return data_fetcher.get_daily(symbol)


def get_1hr_bars(symbol):
    return data_fetcher.get_1hr_bars(symbol)


def get_4hr_bars(symbol):
    return data_fetcher.get_4hr_bars(symbol)


def get_current_price(symbol):
    return data_fetcher.get_current_price(symbol)


# SPX/NDX have no tradable feed here (Alpaca carries ETF options only), so we
# derive their levels from the ETF proxies for trend / RS context only.
INDEX_PROXY = {
    "SPX": ("SPY", 10.0),    # SPX ~= SPY x 10
    "NDX": ("QQQ", 41.0),    # NDX ~= QQQ x ~41 (approx; labeled as proxy)
}


def index_context():
    """Display-only SPX/NDX context: derived level + intraday % change + RS.

    Never produces a tradable signal. The numbers come from the SPY/QQQ proxies
    so the dashboard / chat can frame index direction without an index feed.
    Returns a list of dicts, one per index product.
    """
    out = []
    try:
        spy_chg = get_spy_change()
    except Exception:
        spy_chg = 0.0
    for idx in INDEX_PRODUCTS:
        proxy, factor = INDEX_PROXY.get(idx, (None, None))
        if not proxy:
            continue
        try:
            px = get_current_price(proxy)
            bars = get_intraday(proxy)
            chg = get_symbol_change(bars)
            level = round(px * factor, 2) if px else None
            out.append({
                "symbol":   idx,
                "proxy":    proxy,
                "level":    level,
                "pct":      chg,
                "rs":       relative_strength(chg, spy_chg),
                "is_proxy": True,
            })
        except Exception:
            out.append({"symbol": idx, "proxy": proxy, "level": None,
                        "pct": None, "rs": None, "is_proxy": True})
    return out


def render_index_context_strip():
    """Small horizontal SPX/NDX context strip (display-only, no signal cards)."""
    ctx = index_context()
    if not ctx:
        return ""
    cells = ""
    for c in ctx:
        pct = c.get("pct")
        col = "#8b949e" if pct is None else ("#3fb950" if pct >= 0 else "#f85149")
        lvl = c.get("level")
        cells += (
            "<span style='margin-right:16px'>"
            "<b style='color:#e6edf3'>{sym}</b> "
            "<span style='font-family:monospace'>{lvl}</span> "
            "<span style='color:{col}'>{pct}</span> "
            "<span style='color:#6e7681;font-size:9px'>via {px}</span>"
            "</span>"
        ).format(
            sym=c["symbol"],
            lvl="{:,.2f}".format(lvl) if lvl else "-",
            col=col,
            pct="{:+.2f}%".format(pct) if pct is not None else "-",
            px=c.get("proxy", "-"),
        )
    return (
        "<div style='background:#0d1117;border:1px solid #30363d;border-radius:8px;"
        "padding:8px 12px;margin-bottom:10px;font-size:11px'>"
        "<span style='color:#8b949e;text-transform:uppercase;letter-spacing:.6px;"
        "font-size:9px;font-weight:700;margin-right:12px'>Index Context</span>"
        + cells +
        "<span style='color:#6e7681;font-size:9px;margin-left:6px'>"
        "(proxy levels &mdash; context only, trade SPY/QQQ)</span></div>"
    )


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
                      time_vol_ratio, vol_mult, symbol="?", vol_data_ok=True):
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

    # Volume filter. When the bar-of-day volume ratio is unavailable,
    # get_time_vol_ratio returns the 1.0 sentinel ("N/A") -- which would
    # otherwise sail past the >= 0.9 gate as if volume were confirmed.
    # VWAP_TREND is a volume-confirmed continuation play, so refuse to
    # fire on a placeholder rather than emit an unconfirmed signal.
    if not vol_data_ok:
        return None
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
        symbol, direction, vwap_dist_pct, trend_pct * 100, time_vol_ratio))

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
                                time_vol_ratio, vol_mult, vol_data_ok=True):
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

    # Volume filter. Skip on the N/A volume sentinel (see detect_vwap_trend)
    # so the snap-back doesn't fire without real participation data.
    if not vol_data_ok:
        return None
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
                        time_vol_ratio, vol_mult, market_bias, vol_data_ok=True):
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

    # Volume confirmation. The N/A sentinel (1.0) already fails the >= 1.2
    # gate below, but guard explicitly so intent is clear and the rule
    # holds if the default is ever lowered.
    if not vol_data_ok:
        return None
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
                     et_hour, symbol=None, spot_price=None, conviction=1.0):
    """
    Scores 0-100 across 5 factors using AI-tunable weights from SCANNER_CONFIG.

    If `symbol` and `spot_price` are provided, also computes BONUS points
    from three new edge sources (OI delta, Market Profile, options flow).
    Bonus points are added on top of base score, capped to keep grade<=100.

    Returns: (grade_letter, total_pts, color, breakdown_dict)
      breakdown_dict explains each component for /grade-debug endpoint.
    """
    cfg = get_config()
    pts = 0
    breakdown = {"base_components": {}, "edge_bonuses": {}}

    # 1. Breakout strength
    bs_pct = breakout_strength * 100
    w      = cfg["weight_breakout"]
    bs_pts = 0
    if bs_pct >= cfg["bs_strong"]:
        bs_pts = w
    elif bs_pct >= cfg["bs_medium"]:
        bs_pts = int(w * 0.72)
    elif bs_pct >= cfg["bs_weak"]:
        bs_pts = int(w * 0.48)
    else:
        bs_pts = int(w * 0.24)
    pts += bs_pts
    breakdown["base_components"]["breakout"] = bs_pts

    # 2. Volume ratio
    w = cfg["weight_volume"]
    vp = 0
    if vol_ratio >= cfg["vol_high"]:
        vp = w
    elif vol_ratio >= cfg["vol_med"]:
        vp = int(w * 0.75)
    elif vol_ratio >= cfg["vol_low"]:
        vp = int(w * 0.50)
    else:
        vp = int(w * 0.20)
    pts += vp
    breakdown["base_components"]["volume"] = vp

    # 3. Gap alignment
    w = cfg["weight_gap"]
    gp = 0
    if direction == "CALL":
        if gap_direction == "UP" and gap_pct >= 0.5:
            gp = w
        elif gap_direction == "UP":
            gp = int(w * 0.70)
        elif gap_direction == "FLAT":
            gp = int(w * 0.40)
        else:
            gp = int(w * 0.10)
    else:
        if gap_direction == "DOWN" and abs(gap_pct) >= 0.5:
            gp = w
        elif gap_direction == "DOWN":
            gp = int(w * 0.70)
        elif gap_direction == "FLAT":
            gp = int(w * 0.40)
        else:
            gp = int(w * 0.10)
    pts += gp
    breakdown["base_components"]["gap"] = gp

    # 4. Relative strength
    w = cfg["weight_rs"]
    rp = 0
    if direction == "CALL":
        if rs >= 0.3:    rp = w
        elif rs >= 0.1:  rp = int(w * 0.70)
        elif rs >= -0.1: rp = int(w * 0.40)
        else:            rp = int(w * 0.10)
    else:
        if rs <= -0.3:   rp = w
        elif rs <= -0.1: rp = int(w * 0.70)
        elif rs <= 0.1:  rp = int(w * 0.40)
        else:            rp = int(w * 0.10)
    pts += rp
    breakdown["base_components"]["rs"] = rp

    # 5. Time of day
    w = cfg["weight_time"]
    tp = 0
    if et_hour < cfg["time_prime_end"]:
        tp = w
    elif et_hour < cfg["time_decent_end"]:
        tp = int(w * 0.67)
    elif et_hour < cfg["time_risky_end"]:
        tp = int(w * 0.33)
    else:
        tp = int(w * 0.07)
    pts += tp
    breakdown["base_components"]["time"] = tp

    # Apply vol regime modifier
    pts = int(pts * vol_mult)
    breakdown["base_total"] = pts

    # =============================================
    # NEW EDGE BONUSES (Tier 3) — optional, only if symbol provided
    # =============================================
    edge_pts = 0
    if symbol:
        # OI Delta bonus: -15 to +15
        if HAS_OI_DELTA:
            try:
                delta = oi_delta.compute_delta(symbol)
                if delta:
                    sig = oi_delta.classify_oi_signal(delta, spot_price)
                    # Apply directional alignment: bonus if signal matches our direction
                    raw_bonus = sig.get("grade_pts", 0)
                    if direction == "CALL" and sig["label"] == "BULLISH_BUILD":
                        edge_pts += raw_bonus
                        breakdown["edge_bonuses"]["oi_delta"] = {
                            "pts": raw_bonus, "label": sig["label"]}
                    elif direction == "PUT" and sig["label"] == "BEARISH_BUILD":
                        edge_pts += raw_bonus
                        breakdown["edge_bonuses"]["oi_delta"] = {
                            "pts": raw_bonus, "label": sig["label"]}
                    elif direction == "CALL" and sig["label"] == "BEARISH_BUILD":
                        # Counter-positioning — small penalty
                        edge_pts -= 5
                        breakdown["edge_bonuses"]["oi_delta"] = {
                            "pts": -5, "label": "counter-positioned"}
                    elif direction == "PUT" and sig["label"] == "BULLISH_BUILD":
                        edge_pts -= 5
                        breakdown["edge_bonuses"]["oi_delta"] = {
                            "pts": -5, "label": "counter-positioned"}
            except Exception:
                pass

        # Market Profile bonus: -10 to +12
        # Applies only to SPY/QQQ since profile is built from ES
        if HAS_MPROFILE and symbol in ("SPY", "QQQ") and spot_price:
            try:
                et = pytz.timezone("America/New_York")
                yesterday = datetime.now(et).date() - timedelta(days=1)
                while yesterday.weekday() >= 5:
                    yesterday -= timedelta(days=1)
                prior = market_profile.load_profile("ES", yesterday, "RTH")
                if prior:
                    # Map SPY price to ES rough scale (SPY × 10 ≈ ES)
                    es_proxy_price = spot_price * 10 if symbol == "SPY" else spot_price * 25
                    classification = market_profile.classify_opening(
                        es_proxy_price, prior)
                    if classification:
                        cbias = classification["bias"]
                        cpts  = classification["grade_pts"]
                        # Apply directional alignment
                        if direction == "CALL" and cbias in ("BULL", "NEUTRAL_BULL"):
                            edge_pts += cpts
                            breakdown["edge_bonuses"]["market_profile"] = {
                                "pts": cpts, "class": classification["class"]}
                        elif direction == "PUT" and cbias in ("BEAR", "NEUTRAL_BEAR"):
                            edge_pts += cpts
                            breakdown["edge_bonuses"]["market_profile"] = {
                                "pts": cpts, "class": classification["class"]}
                        elif direction == "CALL" and cbias == "BEAR":
                            edge_pts -= cpts
                            breakdown["edge_bonuses"]["market_profile"] = {
                                "pts": -cpts, "class": "fighting profile"}
                        elif direction == "PUT" and cbias == "BULL":
                            edge_pts -= cpts
                            breakdown["edge_bonuses"]["market_profile"] = {
                                "pts": -cpts, "class": "fighting profile"}
            except Exception:
                pass

        # Options Flow bonus: -15 to +15
        # Only for SPY/QQQ trades AND only during/after opening window
        if HAS_OPT_FLOW and symbol in ("SPY", "QQQ") and et_hour >= 10.0:
            try:
                et = pytz.timezone("America/New_York")
                today = datetime.now(et).date().isoformat()
                flow = options_flow.load_flow(symbol, today)
                if flow:
                    fsig = options_flow.classify_flow(flow)
                    raw = fsig.get("grade_pts", 0)
                    if direction == "CALL" and fsig["label"] == "CALL_AGGRESSIVE":
                        edge_pts += raw
                        breakdown["edge_bonuses"]["opt_flow"] = {
                            "pts": raw, "label": fsig["label"]}
                    elif direction == "PUT" and fsig["label"] == "PUT_AGGRESSIVE":
                        edge_pts += raw
                        breakdown["edge_bonuses"]["opt_flow"] = {
                            "pts": raw, "label": fsig["label"]}
                    elif direction == "CALL" and fsig["label"] == "PUT_AGGRESSIVE":
                        edge_pts -= raw
                        breakdown["edge_bonuses"]["opt_flow"] = {
                            "pts": -raw, "label": "counter-flow"}
                    elif direction == "PUT" and fsig["label"] == "CALL_AGGRESSIVE":
                        edge_pts -= raw
                        breakdown["edge_bonuses"]["opt_flow"] = {
                            "pts": -raw, "label": "counter-flow"}
            except Exception:
                pass

    breakdown["edge_total"] = edge_pts
    pts += edge_pts

    # Conviction weight (regime x GEX). A score weight, not a position size:
    # favorable backdrops (>1) lift the grade, hostile ones (<1) damp it.
    if conviction and conviction != 1.0:
        pts = int(pts * conviction)
    breakdown["conviction"] = conviction

    pts = max(0, min(pts, 100))
    breakdown["final_pts"] = pts

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

    return grade, pts, color, breakdown


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


def check_clear_air(price, direction, t1, t2, key_levels, tol_pct=0.0):
    """
    Checks if key levels block T1 or T2.
    Returns dict: clear_to_t1, clear_to_t2, blocking_level, context

    tol_pct: levels within this % of current price are considered already
    tested / within intraday noise and are ignored, so a level a hair
    overhead doesn't spuriously block the path to T1.
    """
    if not key_levels or not t1 or not t2:
        return {"clear_to_t1": True, "clear_to_t2": True,
                "blocking_level": None, "context": "No levels identified"}

    tol_abs = price * (tol_pct / 100.0) if tol_pct else 0.0

    blocking = []
    if direction == "CALL":
        for lvl in key_levels:
            if price < lvl["price"] <= t2 and (lvl["price"] - price) > tol_abs:
                blocking.append(lvl)
        blocking.sort(key=lambda x: x["price"])
    else:
        for lvl in key_levels:
            if t2 <= lvl["price"] < price and (price - lvl["price"]) > tol_abs:
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
    if not bars_1hr or len(bars_1hr) < 5:
        return "MIXED", "Insufficient 1hr data", 0.0

    # Fractal pivots: 1-bar lookback on each side. The previous 2-bar
    # symmetry rule was too strict on 30 bars of 1hr data and returned
    # "Not enough 1hr pivots" for every symbol in choppy tape.
    highs, lows = [], []
    for i in range(1, len(bars_1hr) - 1):
        b = bars_1hr[i]
        if b["h"] > bars_1hr[i-1]["h"] and b["h"] > bars_1hr[i+1]["h"]:
            highs.append(b["h"])
        if b["l"] < bars_1hr[i-1]["l"] and b["l"] < bars_1hr[i+1]["l"]:
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

def get_time_vol_ratio(intraday_today, daily_bars, current_bar_idx, symbol=None):
    """
    True bar-of-day volume ratio.

    UPGRADED: uses volume_truth module's 30-day cached bar-of-day median
    when available. Falls back to the legacy time-weighted approximation
    if cache is missing or volume_truth module unavailable.

    Returns: (ratio: float, label: str)
    """
    if not intraday_today:
        return 1.0, "N/A"

    current_bar_idx = max(0, min(current_bar_idx, len(intraday_today) - 1))
    current_vol     = intraday_today[current_bar_idx]["v"]

    # --- Path 1: true bar-of-day from cache ---
    if HAS_VOLUME_TRUTH and symbol:
        try:
            ratio, label, _pct = volume_truth.get_true_volume_ratio(
                symbol, current_bar_idx, current_vol)
            if label != "N/A":
                # Re-format label to match legacy style with multiplier
                pretty = "{} ({:.1f}x)".format(label.title(), ratio)
                return ratio, pretty
        except Exception as e:
            log("volume_truth lookup failed for {}: {}".format(symbol, e))
            # fall through to legacy

    # --- Path 2: legacy approximation (fallback) ---
    if not daily_bars or len(daily_bars) < 5:
        return 1.0, "N/A"

    avg_daily_vol = statistics.mean([b["v"] for b in daily_bars[-10:]])
    if avg_daily_vol <= 0:
        return 1.0, "N/A"

    day_fraction  = max(0.01, (current_bar_idx + 1) / 78.0)
    expected_vol  = avg_daily_vol * (day_fraction ** 0.7)
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


def get_liquid_option(symbol, direction, underlying_price=None,
                      et_hour=None, zero_dte_cutoff=None):
    """
    Fetch the nearest available ATM option via Alpaca options snapshot API.

    SPY/QQQ have daily 0DTE options.
    Individual stocks (TSLA, AMD, etc.) only have weekly options (Fri expiry).
    We try today first, then fall back up to 5 trading days out.

    Past the 0DTE cutoff (et_hour >= zero_dte_cutoff) we do NOT select a 0DTE
    contract: there's too little time left for the trade to clear stops, and
    the EOD chain collapses to penny lottery quotes (mid ~$0.06). Rather than
    pick a worthless contract that just gets demoted to WATCHING -- and rather
    than silently rolling into overnight 1DTE risk -- we return "no contract"
    once we've confirmed a same-day chain exists, so the caller keeps the row
    on the board as WATCHING with no trade.

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
        # ±3% window. ±2% was too narrow on end-of-day 0DTE where the
        # true 0.40-delta strike often sits 2-3% OTM, forcing the
        # picker into ITM strikes (delta 0.5-0.7).
        lo = round(underlying_price * 0.97, 2)
        hi = round(underlying_price * 1.03, 2)
    else:
        lo, hi = None, None

    headers = {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    url = "https://data.alpaca.markets/v1beta1/options/snapshots/{}".format(symbol)

    expiry_dates = _next_expiry_dates(n=5)
    skipped = []  # compact per-expiry skip reasons, logged once if we fail

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

            if r.status_code == 422:
                skipped.append("{}:no-chain".format(expiry_str))
                continue
            if r.status_code != 200:
                log("  Options error {} for {} {}: {}".format(
                    r.status_code, symbol, option_type, r.text[:150]))
                return None, None, False, None

            snapshots = r.json().get("snapshots", {})

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
                skipped.append("{}:{}c-none-liquid".format(
                    expiry_str, len(snapshots)))
                continue

            # Alpaca's greeks endpoint returns delta=0.0 for some 0DTE near-
            # ATM contracts (their pricing engine collapses as T -> 0 on
            # expiry day). Without the real delta our 0.40-target selector
            # degrades to strike-distance sorting AND the card displays a
            # misleading delta=0.000. Back-fill from Black-Scholes using the
            # contract's own IV, with a 0.5-day floor on T to keep the math
            # finite (same floor as gamma_exposure.py).
            if underlying_price and any(c["delta"] == 0 for c in candidates):
                exp_date  = _dt.datetime.strptime(expiry_str, "%Y-%m-%d").date()
                dte_days  = (exp_date - today_date).days
                T_years   = max(dte_days, 0.5) / 365.0
                for c in candidates:
                    if c["delta"] > 0:
                        continue
                    iv = c["iv"] if c["iv"] > 0 else 0.50
                    try:
                        d1 = (math.log(underlying_price / c["strike"])
                              + 0.5 * iv * iv * T_years) / (iv * math.sqrt(T_years))
                        n_d1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
                        c["delta"] = round(
                            n_d1 if option_type == "call" else (1.0 - n_d1), 3)
                    except (ValueError, ZeroDivisionError):
                        pass

            if any(c["delta"] > 0 for c in candidates):
                # Asymmetric distance to 0.40 target: ITM overshoot
                # (delta > 0.40) costs 1.5x to nudge selection toward
                # OTM when distances are otherwise comparable. Secondary
                # key prefers lower delta on exact ties.
                def _delta_key(x):
                    d    = x["delta"]
                    dist = abs(d - 0.40)
                    overshoot = max(0.0, d - 0.40)
                    return (dist + 0.5 * overshoot, d)
                candidates.sort(key=_delta_key)
            elif underlying_price:
                candidates.sort(key=lambda x: abs(x["strike"] - underlying_price))

            best = candidates[0]

            exp_date  = _dt.datetime.strptime(expiry_str, "%Y-%m-%d").date()
            dte       = (exp_date - today_date).days
            dte_label = "{}DTE".format(dte) if dte > 0 else "0DTE"

            # Past the 0DTE cutoff: a same-day chain exists but we won't trade
            # it. Return "no contract" (dte_label flags it 0DTE) so the caller
            # keeps the row as WATCHING. We stop here rather than rolling to a
            # later expiry -- a late-day intraday signal shouldn't silently
            # become an overnight 1DTE position.
            if (dte == 0 and et_hour is not None
                    and zero_dte_cutoff is not None
                    and et_hour >= zero_dte_cutoff):
                return None, None, False, "0DTE"

            log("  Selected {} {}: strike={} delta={:.3f} mid={} ({})".format(
                symbol, option_type, best["strike"], best["delta"],
                best["price"], dte_label))

            return best["price"], best["strike"], True, dte_label

        except Exception as e:
            log("Alpaca options exception {} {}: {}".format(symbol, expiry_str, e))
            return None, None, False, None

    log("  No tradable {} {} contract: {}".format(
        symbol, option_type, ", ".join(skipped) if skipped else "no expiries"))
    return None, None, False, None


def current_week_expiry(now_et=None, zero_dte_cutoff=None):
    """The weekly tier's expiry: THIS week's Friday settlement, ridden all week.

    Returns (date_str, dte). Monday -> this Friday (~4 DTE), counting down to
    0DTE on Friday itself, then rolls to next Friday. This keeps weekly alerts
    pinned to one settlement for the whole week instead of emitting a fresh
    7-days-out contract every day. On Friday past the 0DTE cutoff (or over the
    weekend) it rolls forward to next Friday.
    """
    import datetime as _dt
    et = pytz.timezone("America/New_York")
    now = now_et or datetime.now(et)
    today = now.date()
    wd = today.weekday()                 # Mon=0 .. Sun=6
    days_ahead = (4 - wd) % 7            # this week's Friday (0 if today is Fri)
    friday = today + _dt.timedelta(days=days_ahead)

    cutoff = zero_dte_cutoff if zero_dte_cutoff is not None else 14.5
    et_hour = now.hour + now.minute / 60.0
    # Friday after the cutoff: this week's settlement is effectively done --
    # roll to next Friday so we don't keep firing a dead 0DTE.
    if wd == 4 and et_hour >= cutoff:
        friday = friday + _dt.timedelta(days=7)

    dte = (friday - today).days
    return friday.strftime("%Y-%m-%d"), dte


def get_swing_option(symbol, direction, underlying_price=None):
    """ATM-ish (~0.40 delta) option on the current-week Friday settlement.

    The weekly tier's option picker. Mirrors get_liquid_option's Alpaca
    snapshot machinery but targets a single fixed expiry (current_week_expiry)
    so a weekly signal rides one contract through the week.

    Returns (premium, strike, is_live, dte_label, volume) where volume is the
    contract's Alpaca daily option volume (None if unavailable).
    """
    import datetime as _dt

    if not ALPACA_KEY or not ALPACA_SECRET:
        return None, None, False, None, None

    cfg = get_config()
    expiry_str, dte = current_week_expiry(
        zero_dte_cutoff=cfg.get("zero_dte_cutoff_hour", 14.5))
    dte_label = "{}DTE".format(dte) if dte > 0 else "0DTE"

    option_type = "call" if direction == "CALL" else "put"
    headers = {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    url = "https://data.alpaca.markets/v1beta1/options/snapshots/{}".format(symbol)

    params = {
        "feed":            "indicative",
        "expiration_date": expiry_str,
        "type":            option_type,
        "limit":           50,
    }
    if underlying_price:
        params["strike_price_gte"] = round(underlying_price * 0.94, 2)
        params["strike_price_lte"] = round(underlying_price * 1.06, 2)

    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        if r.status_code != 200:
            return None, None, False, None, None
        snapshots = r.json().get("snapshots", {}) or {}

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
            # Alpaca daily option volume (the field the quote-only picker used
            # to ignore) -- the volume side of the vol/OI confluence check.
            day    = snap.get("dailyBar") or {}
            vol    = day.get("v")
            candidates.append({"strike": strike, "price": mid, "delta": delta,
                               "volume": int(vol) if vol is not None else None})

        if not candidates:
            return None, None, False, dte_label, None

        if any(c["delta"] > 0 for c in candidates):
            candidates.sort(key=lambda x: abs(x["delta"] - 0.40))
        elif underlying_price:
            candidates.sort(key=lambda x: abs(x["strike"] - underlying_price))
        best = candidates[0]
        return best["price"], best["strike"], True, dte_label, best.get("volume")
    except Exception as e:
        log("Swing option exception {} {}: {}".format(symbol, expiry_str, e))
        return None, None, False, None, None


# =============================================
# SWING ENGINE -- DATA FETCHERS
# =============================================

# Daily/weekly bars barely change intraday, yet the swing scan re-pulls the
# full 72-symbol universe (x2 timeframes) every few minutes in bursts of 8
# threads. That hammers Alpaca's rate limiter -- a single 429 on the first
# symbols cascades into a whole-universe "data:0/72" outage. A short-TTL cache
# means the burst only happens once; a much longer stale window lets a
# transient outage reuse the last good bars instead of zeroing the scan.
_BARS_CACHE      = {}
_BARS_CACHE_LOCK = threading.Lock()
_BARS_FRESH_TTL  = 30 * 60       # serve from cache without re-fetching
_BARS_STALE_TTL  = 6 * 3600      # last-resort reuse when a fetch fails


def _bars_cache_get(key, max_age):
    with _BARS_CACHE_LOCK:
        entry = _BARS_CACHE.get(key)
        if entry and (time.time() - entry[0]) <= max_age:
            return entry[1]
    return None


def _bars_cache_set(key, bars):
    with _BARS_CACHE_LOCK:
        _BARS_CACHE[key] = (time.time(), bars)


def _fetch_bars(symbol, timeframe, limit, kind):
    """
    Cached, rate-limit-aware bar fetch shared by the daily/weekly getters.

    Order of preference: fresh cache -> live fetch (with 429 backoff) ->
    stale cache. Records each non-200 status into the swing stats so a
    0-setup scan can report the dominant cause instead of swallowing it.
    """
    cache_key = "{}|{}|{}".format(symbol, timeframe, limit)

    fresh = _bars_cache_get(cache_key, _BARS_FRESH_TTL)
    if fresh is not None:
        return fresh

    # Be explicit about feed + window. On the free Alpaca plan the bars
    # endpoint defaults to the SIP feed, which a free key isn't entitled to --
    # the request still returns HTTP 200 but with an empty `bars` array, so a
    # whole-universe sweep silently reports data:0/N with no error status. Pin
    # feed=iex (the free entitlement) and pass an explicit start date wide
    # enough to cover `limit` bars so Alpaca never short-changes the window.
    if timeframe == "1Week":
        _lookback_days = int(limit * 7 * 1.4) + 14
    elif timeframe == "1Day":
        _lookback_days = int(limit * 1.6) + 10      # trading->calendar slack
    else:
        _lookback_days = 7
    _start_iso = (datetime.now(pytz.utc)
                  - timedelta(days=_lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {"timeframe": timeframe, "limit": limit,
              "feed": "iex", "start": _start_iso}

    attempts = 3
    backoff  = 0.5
    for attempt in range(attempts):
        try:
            r = requests.get(DATA_URL.format(symbol), headers=HEADERS,
                             params=params,
                             timeout=10)
            if r.status_code == 200:
                bars = r.json().get("bars", []) or []
                if bars:
                    _bars_cache_set(cache_key, bars)
                    return bars
                # 200 OK but no bars. This is the silent-outage signature
                # (feed/entitlement or a too-narrow window). Record it
                # distinctly so the universe-OUTAGE line names the real cause
                # instead of "no HTTP status captured", then drop to the
                # stale-cache fallback below (retrying an empty 200 is futile).
                _swing_stats_bump("http_200_empty")
                _log_swing_fetch_err(symbol, kind + "/empty",
                                     "200 OK but bars=[] (feed=iex/subscription/window?)")
                break
            _swing_stats_bump("http_{}".format(r.status_code))
            # 429 = rate limited: back off and retry before giving up.
            if r.status_code == 429 and attempt < attempts - 1:
                time.sleep(backoff)
                backoff *= 2
                continue
            _log_swing_fetch_err(symbol, kind + "/http",
                                 "{} {}".format(r.status_code, r.text[:120]))
            break
        except Exception as e:
            _swing_stats_bump("http_exc")
            if attempt < attempts - 1:
                time.sleep(backoff)
                backoff *= 2
                continue
            _log_swing_fetch_err(symbol, kind + "/exc",
                                 "{}: {}".format(type(e).__name__, str(e)[:120]))
            break

    # Live fetch failed -- reuse the last good bars if we have any.
    stale = _bars_cache_get(cache_key, _BARS_STALE_TTL)
    if stale is not None:
        _swing_stats_bump("served_stale")
    return stale


def get_daily_extended(symbol, limit=90):
    return _fetch_bars(symbol, "1Day", limit, "daily")


def get_weekly_bars(symbol, limit=52):
    return _fetch_bars(symbol, "1Week", limit, "weekly")


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

def option_risk_levels(premium):
    """Option-premium stop / target for a single contract.

    Position size is no longer part of the model, so this only returns the
    premium-based exit levels (stop ~45% below, target ~40% above) the scanner
    previously bundled into calculate_contracts. Returns (stop, target).
    """
    if not premium or premium <= 0:
        return None, None
    return round(premium * 0.55, 2), round(premium * 1.4, 2)


def current_conviction():
    """Regime x GEX conviction weight (replaces the old size multiplier).

    This is a *score weight*, not a position size: a value >1 means the active
    regime / gamma backdrop favors the setup, <1 means it's hostile. Computed
    from the shared market state so every signal in a scan shares one value.
    """
    with _market_state_lock:
        regime   = _market_state.get("regime")
        gex_bias = _market_state.get("gex_bias")
    conv = 1.0
    if regime and regime.get("rules"):
        conv *= regime["rules"].get("conviction_multiplier", 1.0)
    if gex_bias:
        conv *= gex_bias.get("conviction_mult", 1.0)
    return conv


def detect_orb_pullback_continuation(intraday, vwap, orb_high, orb_low,
                                     direction, orb_range, cfg,
                                     time_vol_ratio, vol_data_ok, orb_bars):
    """Trend-day continuation entry for an ORB breakout that has run extended.

    Anti-chase correctly refuses to buy a bar that is already extended past the
    trigger. But on a trend day that suppresses *every* signal. This detects the
    one high-probability way back in: the breakout ran, then pulled back toward
    the trigger and *held* it (a higher low), and the current bar is resuming in
    the trend direction on real volume. Returns a dict with the continuation
    stop (just under the held level) or None if it's still ripping / has failed.
    """
    post_orb = intraday[orb_bars:]
    if len(post_orb) < 4:
        return None
    recent = post_orb[-8:]
    last, prev = intraday[-1], intraday[-2]
    price      = last["c"]
    min_depth  = orb_range * cfg.get("pullback_min_depth", 0.25)
    ext_thresh = orb_range * cfg.get("max_breakout_extension", 0.6)
    vol_min    = cfg.get("pullback_vol_min", 1.1)

    if not (vol_data_ok and time_vol_ratio >= vol_min):
        return None

    if direction == "CALL":
        # Locate the extension peak, then measure the dip in the bars *after*
        # it -- a resuming bar recovers toward the peak, so dip depth must be
        # read from the peak-to-trough, not peak-to-current-price.
        highs       = [b["h"] for b in recent]
        hi_idx      = max(range(len(recent)), key=lambda i: highs[i])
        recent_high = highs[hi_idx]
        after       = recent[hi_idx + 1:]
        if not after:                       # peak is the latest bar = still ripping
            return None
        dip_low = min(b["l"] for b in after)
        # 1) the move actually extended (trend day, not a fresh poke)
        if recent_high - orb_high < ext_thresh:
            return None
        # 2) a genuine pullback off the peak (not a one-tick wobble)
        if recent_high - dip_low < min_depth:
            return None
        # 3) the trigger held as support -- higher low above the breakout level
        if dip_low < orb_high:
            return None
        # 4) current bar resuming up, back above VWAP
        if not (last["c"] > last["o"] and last["c"] >= prev["c"] and price > vwap):
            return None
        stop = round(dip_low - orb_range * 0.15, 2)
        return {"ok": True, "stop": stop,
                "note": "pullback to {:.2f} held > ORB-high {:.2f}, resuming on {:.1f}x vol".format(
                    dip_low, orb_high, time_vol_ratio)}
    else:
        lows       = [b["l"] for b in recent]
        lo_idx     = min(range(len(recent)), key=lambda i: lows[i])
        recent_low = lows[lo_idx]
        after      = recent[lo_idx + 1:]
        if not after:
            return None
        dip_high = max(b["h"] for b in after)
        if orb_low - recent_low < ext_thresh:
            return None
        if dip_high - recent_low < min_depth:
            return None
        if dip_high > orb_low:
            return None
        if not (last["c"] < last["o"] and last["c"] <= prev["c"] and price < vwap):
            return None
        stop = round(dip_high + orb_range * 0.15, 2)
        return {"ok": True, "stop": stop,
                "note": "pullback to {:.2f} held < ORB-low {:.2f}, resuming on {:.1f}x vol".format(
                    dip_high, orb_low, time_vol_ratio)}


# =============================================
# SCANNER
# =============================================

def scan_all_symbols():
    results = []
    cfg = get_config()  # snapshot config for this entire scan

    # --- Pull current regime + GEX bias from shared state (refreshed daily) ---
    with _market_state_lock:
        regime    = _market_state.get("regime")
        premarket = _market_state.get("premarket_brief")
        gex_bias  = _market_state.get("gex_bias")

    # Strategy enable/disable matrix (defaults to all-on if regime missing)
    strat_rules = {
        "orb": True, "vwap_trend": True, "vwap_mr": True, "ib_extension": True,
        "conviction_multiplier": 1.0,
    }
    if regime and "rules" in regime:
        strat_rules.update(regime["rules"])
    # GEX override: very strong gamma regime can flip individual strategies
    if gex_bias:
        if gex_bias.get("tape_bias") == "STRONG_MEAN_REVERT":
            strat_rules["orb"]        = False
            strat_rules["vwap_trend"] = False
        elif gex_bias.get("tape_bias") == "STRONG_TREND":
            strat_rules["vwap_mr"]    = False
        # Conviction weight compounds with regime
        strat_rules["conviction_multiplier"] *= gex_bias.get("conviction_mult", 1.0)

    # One conviction weight shared by every signal in this scan (score weight,
    # not position size).
    scan_conviction = strat_rules.get("conviction_multiplier", 1.0)

    if regime:
        _regime_line = "Regime: {} (VIX {}) | conviction x{:.2f} | {}".format(
            regime.get("regime"), regime.get("vix"),
            strat_rules["conviction_multiplier"], regime.get("note", ""))
        if regime.get("vix") is None:
            # Throttle: only log on transition into / out of "VIX unavailable"
            # so the warn doesn't repeat every 5-min scan.
            log_state_transition(
                "regime.vix", "unavailable",
                _regime_line + " (VIX unavailable — using fallback)")
        else:
            log_state_transition("regime.vix", "ok",
                                 "Regime VIX restored: " + _regime_line,
                                 level="info")
            log(_regime_line)
    if gex_bias:
        _note = gex_bias.get("note", "")
        gex_unhealthy = (gex_bias.get("gex_b") is None
                         or gex_bias.get("regime") == "UNKNOWN")
        # If the Databento billing breaker is tripped, surface the actual cause
        # instead of the generic "No GEX data — defaulting to trend-mode" line,
        # which made it look like a normal regime decision in today's logs.
        if gex_unhealthy and _databento_blocked():
            import databento_adapter as _da
            _note = ("GEX unavailable: Databento billing breaker engaged "
                     "until {}").format(_da.billing_status().get("blocked_until"))
        _gex_line = "GEX: ${}B {} | {}".format(
            gex_bias.get("gex_b"), gex_bias.get("regime"), _note)
        if gex_unhealthy:
            log_state_transition("gex.bias", "unhealthy", _gex_line)
        else:
            log_state_transition("gex.bias", "ok",
                                 "GEX restored: " + _gex_line, level="info")
            log(_gex_line)

    # Warm all bar caches in parallel before the per-symbol loop.
    # Intraday ETF universe + SPY/QQQ for the market-alignment block below.
    data_fetcher.prefetch_symbols(list(set(INTRADAY_SYMBOLS) | {"SPY", "QQQ"}))

    # IB-extension unlock: if SPY has cleanly broken its 30-min initial balance
    # with volume confirmation, re-enable ORB/vwap_trend/ib_extension at full
    # grading regardless of the morning regime label.
    spy_bars = get_intraday("SPY")
    unlock_dir = _check_ib_extension_unlock(spy_bars)
    if unlock_dir or _ib_unlock_state.get("unlocked"):
        for k in ("orb", "vwap_trend", "ib_extension"):
            strat_rules[k] = True
        # Drop the regime penalty -- the unlock IS our expansion confirmation.
        strat_rules.pop("score_penalty_trend", None)
        # Bump conviction back to LOW_VOL baseline once expansion is confirmed.
        strat_rules["conviction_multiplier"] = max(
            strat_rules.get("conviction_multiplier", 1.0), 0.85)
        scan_conviction = strat_rules["conviction_multiplier"]

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

    for symbol in INTRADAY_SYMBOLS:
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
            "horizon": "INTRADAY", "tier": 0,
            "product_class": "ETF" if symbol in ETF_PRODUCTS else "STOCK",
            "conviction": scan_conviction,
        }

        intraday = get_intraday(symbol)
        daily    = get_daily(symbol)
        bars_1hr = get_1hr_bars(symbol)
        bars_4hr = get_4hr_bars(symbol)

        # ORB window = first 30 min of RTH (9:30 - 10:00 ET). Even with
        # a valid bar count, never fire an ORB signal before the opening
        # range has fully formed -- gives a known-good high/low to break.
        et          = pytz.timezone("America/New_York")
        et_now      = datetime.now(et)
        et_minutes  = et_now.hour * 60 + et_now.minute
        ORB_DONE_ET = 10 * 60   # 10:00 AM ET in minutes-of-day

        if et_minutes < ORB_DONE_ET:
            result["status"] = "pre-ORB ({}m to go)".format(ORB_DONE_ET - et_minutes)
            results.append(result); continue

        if not intraday or len(intraday) < ORB_BARS + 1 or not daily:
            result["status"] = "no data"; results.append(result); continue

        # Stale-price guard. On free Alpaca (IEX-only feed) the latest
        # quote/trade can be minutes old for non-IEX-heavy names. When
        # that happens we try Yahoo Finance as a rescue inside
        # is_price_stale(); if Yahoo agrees with prior close within
        # +/- 20% we accept it and override the (stale) IEX bar close
        # downstream so entry/strike/target math uses the real spot.
        stale, fresh_price, age_s, source = data_fetcher.is_price_stale(symbol)
        if stale:
            if age_s is None:
                result["status"] = "no price"
            else:
                result["status"] = "stale price ({}s old from {})".format(
                    int(age_s), source or "?")
            results.append(result); continue
        result["price_source"] = source

        # --- 0DTE earnings flag (don't block, just warn) ---
        if HAS_SAFETY_GATES:
            try:
                _allowed, _reason = safety_gates.earnings_filter(symbol, "0dte")
                if "HIGH_RISK" in _reason or "caution" in _reason:
                    result["earnings_warning"] = _reason
            except Exception:
                pass

        vol_mult = volatility_score(daily)
        if vol_mult == 0.0:
            result["status"] = "dead market"; results.append(result); continue

        orb      = intraday[:ORB_BARS]
        orb_high = max(b["h"] for b in orb)
        orb_low  = min(b["l"] for b in orb)
        current  = intraday[-1]
        price    = current["c"]
        # When the latest IEX bar is stale and Yahoo rescued the spot,
        # override the bar close so strike selection, vs_orb_high and
        # entry/target math use the real consolidated-tape price. VWAP
        # and the ORB high/low stay as-is -- they're the day's session
        # data, not "current".
        if source == "yahoo" and fresh_price is not None:
            price = fresh_price
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

        et_hour    = et_now.hour + et_now.minute / 60.0
        late_entry = et_hour >= 14.0

        key_levels  = get_key_levels(daily, bars_1hr, bars_4hr)

        # 1hr trend confirmation
        trend_1hr, trend_desc, trend_score = get_1hr_trend(bars_1hr)

        # Time-adjusted volume (index of current bar in today's session)
        bar_idx                = len(intraday) - 1
        time_vol_ratio, tv_lbl = get_time_vol_ratio(intraday, daily, bar_idx, symbol=symbol)
        # tv_lbl == "N/A" means get_time_vol_ratio had no real volume data and
        # returned the 1.0 sentinel. Volume-confirmed strategies must not treat
        # that placeholder as a passing volume reading.
        vol_data_ok = tv_lbl != "N/A"

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
            stop_mult = cfg.get("orb_stop_mult", 1.0)
            result["und_call_t1"]   = round(price + orb_range, 2)
            result["und_call_t2"]   = round(price + orb_range * 2, 2)
            result["und_call_stop"] = round(price - orb_range * stop_mult, 2)
            result["und_put_t1"]    = round(price - orb_range, 2)
            result["und_put_t2"]    = round(price - orb_range * 2, 2)
            result["und_put_stop"]  = round(price + orb_range * stop_mult, 2)
            avg_range = statistics.mean([b["h"] - b["l"] for b in daily[-10:]])
            if avg_range > 0:
                result["t1_prob"] = round(max(20, min(85, 100 - (orb_range / avg_range * 100))), 0)
                result["t2_prob"] = round(max(10, min(55, 100 - (orb_range * 2 / avg_range * 100))), 0)

        direction = None
        breakout_strength = 0

        orb_enabled = strat_rules.get("orb", True)

        if orb_enabled and price > orb_high and price > vwap:
            direction         = "CALL"
            breakout_strength = (price - orb_high) / orb_high
            result["vs_orb"]  = "+{:.3f}%".format(abs(vs_orb_high))
            result["vs_vwap"] = "+{:.3f}%".format(abs(vs_vwap))
        elif orb_enabled and price < orb_low and price < vwap:
            direction         = "PUT"
            breakout_strength = (orb_low - price) / orb_low
            result["vs_orb"]  = "-{:.3f}%".format(abs(vs_orb_low))
            result["vs_vwap"] = "-{:.3f}%".format(abs(vs_vwap))

        # Anti-chase: an ORB breakout that has already run more than
        # max_breakout_extension * ORB range past the trigger is a late,
        # extended entry -- the stop/target geometry no longer holds and these
        # are a dominant loss source. Demote to WATCHING rather than signal it.
        if direction and orb_range > 0:
            _ext = cfg.get("max_breakout_extension", 0.6)
            if direction == "CALL":
                _run = price - orb_high
            else:
                _run = orb_low - price
            if _run > orb_range * _ext:
                # Before giving up, check for a trend-day pullback continuation:
                # extended move + held trigger + resuming on volume = the high-
                # probability re-entry anti-chase would otherwise mask.
                cont = None
                if cfg.get("pullback_reentry_enabled", True):
                    cont = detect_orb_pullback_continuation(
                        intraday, vwap, orb_high, orb_low, direction,
                        orb_range, cfg, time_vol_ratio, vol_data_ok, ORB_BARS)
                if cont and cont.get("ok"):
                    result["signal_type"] = "ORB_PULLBACK"
                    result["entry_style"] = "pullback"
                    result["vs_orb_run"]  = round(_run / orb_range, 2)
                    if direction == "CALL":
                        result["und_call_stop"] = cont["stop"]
                    else:
                        result["und_put_stop"]  = cont["stop"]
                    log("{}: {} ORB-PULLBACK continuation — {}".format(
                        symbol, direction, cont["note"]))
                    # Fall through to the normal ORB grading path below.
                else:
                    result["status"]     = "extended (anti-chase)"
                    result["vs_orb_run"] = round(_run / orb_range, 2)
                    results.append(result)
                    log("{}: SKIP {} extended {:.2f}x ORB past trigger (anti-chase)".format(
                        symbol, direction, _run / orb_range))
                    continue

        if direction is None:
            # --- No ORB breakout: try alternative strategies ---
            alt_signal = None

            # 1. VWAP Trend Following (Zarattini & Aziz)
            if not alt_signal and strat_rules.get("vwap_trend", True):
                alt_signal = detect_vwap_trend(
                    intraday, daily, vwap, cfg, et_hour,
                    gap_pct, gap_dir, rs, market_bias,
                    time_vol_ratio, vol_mult, symbol=symbol,
                    vol_data_ok=vol_data_ok)

            # 2. VWAP Mean Reversion (deviation band snap-back)
            if not alt_signal and strat_rules.get("vwap_mr", True):
                alt_signal = detect_vwap_mean_reversion(
                    intraday, daily, vwap, cfg, et_hour,
                    time_vol_ratio, vol_mult, vol_data_ok=vol_data_ok)

            # 3. Initial Balance Extension (Market Profile)
            if not alt_signal and strat_rules.get("ib_extension", True):
                alt_signal = detect_ib_extension(
                    intraday, daily, vwap, cfg, et_hour,
                    time_vol_ratio, vol_mult, market_bias,
                    vol_data_ok=vol_data_ok)

            # 4. Opening Drive (overnight inventory + gap alignment) — NEW
            # ETF-only: the premarket brief is an SPY/index overnight-inventory
            # read, so it must not be applied to single-stock symbols.
            if (not alt_signal and HAS_NEW_STRATS and premarket
                    and symbol in ETF_PRODUCTS):
                try:
                    od = new_strategies.detect_opening_drive(
                        intraday_5min   = intraday,
                        vwap            = vwap,
                        premarket_brief = premarket,
                        regime_data     = regime,
                        time_vol_ratio  = time_vol_ratio,
                    )
                    if od:
                        # Adapt the OD signal shape to match the other alt signals
                        alt_signal = {
                            "signal_type": od["signal_type"],
                            "direction":   od["direction"],
                            "score":       od["score"],
                            "t1":          od["t1"],
                            "t2":          od["t2"],
                            "stop":        od["stop"],
                        }
                except Exception as e:
                    log("Opening Drive detect error for {}: {}".format(symbol, e))

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
                result["vol_ratio"] = round(vol_ratio, 2)
                breakout_strength = abs(price - vwap) / vwap if vwap else 0
                score = alt_signal["score"]

                result["aligned"] = not (
                    (direction == "CALL" and market_bias == "BEAR") or
                    (direction == "PUT"  and market_bias == "BULL")
                )

                grade, grade_pts, grade_color, _grade_bd = confluence_grade(
                    breakout_strength, vol_ratio, vol_mult,
                    gap_pct, gap_dir, rs, direction, et_hour,
                    symbol=symbol, spot_price=price, conviction=scan_conviction)

                # Alt strategies get a slight grade bump for passing their own filters
                grade_pts = min(grade_pts + 5, 100)

                # Regime penalty on trend-style ALT signals (COMPRESSED only:
                # raises the bar so chop doesn't trigger but real expansion can).
                if alt_signal["signal_type"] in ("VWAP_TREND", "IB_EXTENSION"):
                    grade_pts = max(0, grade_pts - strat_rules.get("score_penalty_trend", 0))
                    if grade_pts < cfg["grade_a_min"] and grade == "A":
                        grade = "B"; grade_color = "#3fb950"
                    if grade_pts < cfg["grade_b_min"] and grade == "B":
                        grade = "C"; grade_color = "#f0883e"
                    if grade_pts < cfg["grade_c_min"]:
                        grade = "D"; grade_color = "#8b949e"

                t1_key = "und_call_t1" if direction == "CALL" else "und_put_t1"
                t2_key = "und_call_t2" if direction == "CALL" else "und_put_t2"
                clear_air = check_clear_air(price, direction,
                                            result.get(t1_key), result.get(t2_key),
                                            key_levels,
                                            tol_pct=cfg.get("clear_air_tol_pct", 0.0))
                result["clear_air"] = clear_air

                if not clear_air["clear_to_t1"]:
                    blk = clear_air.get("blocking_level") or {}
                    weak = blk.get("strength", 3) <= cfg.get("clear_air_weak_strength", 1)
                    if weak:
                        grade_pts = min(grade_pts, 62)
                        if grade == "A":
                            grade = "B"; grade_color = "#3fb950"
                    else:
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

                premium, strike, is_live, dte_label = get_liquid_option(
                    symbol, direction, price, et_hour=et_hour,
                    zero_dte_cutoff=cfg.get("zero_dte_cutoff_hour", 14.5))

                if premium and is_live:
                    stp, tgt = option_risk_levels(premium)
                    result["premium"]   = round(premium, 2)
                    result["strike"]    = strike
                    result["stop"]      = stp
                    result["target"]    = tgt
                    result["dte_label"] = dte_label or "0DTE"
                    result["status"]    = "SIGNAL"
                else:
                    result["status"] = "SIGNAL (no options)"
                    # Only the suppressed-0DTE sentinel returns a label here;
                    # a genuine no-tradable contract returns None -- don't
                    # mislabel those as 0DTE (would wrongly demote them).
                    if dte_label:
                        result["dte_label"] = dte_label

                # Hard 0DTE late-entry cutoff: too little time for the
                # trade to clear stops before close. Keep the row on
                # the dashboard but as WATCHING so it doesn't fire. Also
                # covers the case where get_liquid_option suppressed the
                # 0DTE pick past cutoff (status "SIGNAL (no options)").
                if (result.get("dte_label") == "0DTE"
                        and et_hour >= cfg.get("zero_dte_cutoff_hour", 14.5)
                        and result["status"] in ("SIGNAL", "SIGNAL (no options)")):
                    result["status"] = "WATCHING"
                    log("{}: 0DTE demoted to WATCHING (et_hour={:.2f} >= {})".format(
                        symbol, et_hour, cfg.get("zero_dte_cutoff_hour", 14.5)))

                result["score"]       = round(score, 2)
                result["grade"]       = grade
                result["grade_pts"]   = grade_pts
                result["grade_color"] = grade_color
                results.append(result)
                log("{} {} | {} {} {}pts | alt_strategy".format(
                    symbol, direction, alt_signal["signal_type"], grade, grade_pts))
                continue

            # No alternative signal either — fall through to WATCHING
            # ONLY if price is within 0.5% of the ORB breakout level.
            # Otherwise this is just noise and doesn't belong on the dashboard.
            result["direction"] = "CALL" if price > vwap else "PUT"
            result["vs_vwap"]   = "{:+.3f}%".format(vs_vwap)
            result["vs_orb"]    = "{:.2f}% from ORB {}".format(
                abs(vs_orb_high if price > vwap else vs_orb_low),
                "high" if price > vwap else "low")

            # Distance from the relevant ORB level
            if result["direction"] == "CALL":
                pct_to_breakout = (orb_high - price) / price * 100
            else:
                pct_to_breakout = (price - orb_low) / price * 100

            # Only watch genuinely-approaching setups
            if 0 <= pct_to_breakout <= 0.5:
                vol_ratio = current["v"] / intraday[-2]["v"] if intraday[-2]["v"] > 0 else 1
                result["score"]  = round((1 - min(abs(vs_orb_high), abs(vs_orb_low)) / 100) * vol_mult * 10, 2)
                result["status"] = "WATCHING"
                results.append(result)
                continue
            else:
                # Too far to watch — bury as 'idle'
                result["status"] = "idle"
                result["score"]  = 0
                results.append(result)
                continue

        vol_ratio = current["v"] / intraday[-2]["v"] if intraday[-2]["v"] > 0 else 1
        result["vol_ratio"] = round(vol_ratio, 2)
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

        grade, grade_pts, grade_color, _grade_bd = confluence_grade(
            breakout_strength, vol_ratio, vol_mult,
            gap_pct, gap_dir, rs, direction, et_hour,
            symbol=symbol, spot_price=price, conviction=scan_conviction)

        # Regime penalty on ORB during COMPRESSED -- enabled but harder to fire.
        # IB-extension breakout override (set earlier in scan_all_symbols) waives
        # the penalty since the unlock signal already confirms expansion.
        if not _ib_unlock_state.get("unlocked"):
            grade_pts = max(0, grade_pts - strat_rules.get("score_penalty_trend", 0))
            if grade_pts < cfg["grade_a_min"] and grade == "A":
                grade = "B"; grade_color = "#3fb950"
            if grade_pts < cfg["grade_b_min"] and grade == "B":
                grade = "C"; grade_color = "#f0883e"
            if grade_pts < cfg["grade_c_min"]:
                grade = "D"; grade_color = "#8b949e"

        t1_key = "und_call_t1" if direction == "CALL" else "und_put_t1"
        t2_key = "und_call_t2" if direction == "CALL" else "und_put_t2"
        clear_air = check_clear_air(price, direction,
                                    result.get(t1_key), result.get(t2_key),
                                    key_levels,
                                    tol_pct=cfg.get("clear_air_tol_pct", 0.0))
        result["clear_air"] = clear_air

        if not clear_air["clear_to_t1"]:
            blk = clear_air.get("blocking_level") or {}
            weak = blk.get("strength", 3) <= cfg.get("clear_air_weak_strength", 1)
            if weak:
                # A lone 1H swing-high is not strong enough for the hard C cap.
                # Apply a softer one-grade penalty and a higher points ceiling.
                grade_pts = min(grade_pts, 62)
                if grade == "A":
                    grade = "B"; grade_color = "#3fb950"
                    log("  {} grade A->B (weak {} blocks T1): {}".format(
                        symbol, blk.get("label", "level"), clear_air["context"]))
            else:
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

        premium, strike, is_live, dte_label = get_liquid_option(
            symbol, direction, price, et_hour=et_hour,
            zero_dte_cutoff=cfg.get("zero_dte_cutoff_hour", 14.5))

        if premium and is_live:
            stp, tgt = option_risk_levels(premium)
            result["premium"]   = round(premium, 2)
            result["strike"]    = strike
            result["stop"]      = stp
            result["target"]    = tgt
            result["dte_label"] = dte_label or "0DTE"
            result["status"]    = "SIGNAL"
        else:
            result["status"] = "SIGNAL (no options)"
            # See alt-strategy path: only label the suppressed-0DTE sentinel.
            if dte_label:
                result["dte_label"] = dte_label

        # Hard 0DTE late-entry cutoff (mirrors alt-strategy path above).
        # Also catches the suppressed-pick case ("SIGNAL (no options)").
        if (result.get("dte_label") == "0DTE"
                and et_hour >= cfg.get("zero_dte_cutoff_hour", 14.5)
                and result["status"] in ("SIGNAL", "SIGNAL (no options)")):
            result["status"] = "WATCHING"
            log("{}: 0DTE demoted to WATCHING (et_hour={:.2f} >= {})".format(
                symbol, et_hour, cfg.get("zero_dte_cutoff_hour", 14.5)))

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

    if not market_open():
        # market.clock already logs the open->closed transition; stay
        # silent on subsequent closed scans instead of emitting the
        # "=== Running signal scan === / Market closed" pair every 5 min.
        with state_lock:
            next_scan_at = time.time() + SCAN_INTERVAL
        return

    log("=== Running signal scan ===")
    _log_auth_state_if_changed()

    # Resolve any live auto positions against underlying targets first; this
    # may free the INTRADAY tier so a fresh alert can fire this scan.
    try:
        monitor_active_positions()
    except Exception as _e:
        log("monitor_active_positions error: {}".format(_e))

    results = scan_all_symbols()

    with state_lock:
        all_signals  = results
        next_scan_at = time.time() + SCAN_INTERVAL

    signals  = [r for r in results if r["status"] == "SIGNAL"]
    watching = [r for r in results if r["status"] == "WATCHING"]

    # Alert quality gate: only A/B grades fire (C is near-random confluence --
    # still scanned and shown on the dashboard, just not alerted). Counter-trend
    # setups are already suppressed at detection when counter_trend_allowed is
    # off, but guard on the aligned flag here too as a belt-and-suspenders.
    _cfg          = get_config()
    _alert_floor  = _cfg.get("alert_min_grade", "B")
    _allowed_gr   = {"A": {"A"}, "B": {"A", "B"}, "C": {"A", "B", "C"}}.get(
        _alert_floor, {"A", "B"})
    alertable = [s for s in signals
                 if s.get("grade") in _allowed_gr and s.get("aligned", True)]
    if signals and not alertable:
        log("Intraday: {} signal(s) below alert floor {} or counter-trend; "
            "no alert".format(len(signals), _alert_floor))

    # One live INTRADAY position at a time: if the tier is occupied, emit no new
    # intraday alerts until it closes (win/loss on underlying targets).
    intraday_locked = tier_has_open_position("INTRADAY")
    if intraday_locked:
        log("INTRADAY tier locked -- live position open, suppressing alerts")

    # Telegram: alert on confirmed signals
    for sig in (alertable if not intraday_locked else []):
        if bot_enabled and should_alert(sig["symbol"], sig["direction"]):
            if HAS_SCANNER_CORE and "rationale" not in sig:
                try:
                    sig["rationale"] = scanner_core.build_rationale(sig)
                except Exception:
                    pass
            db_log_signal(sig)

            # Build context line — regime + GEX bias for situational awareness
            with _market_state_lock:
                _r = _market_state.get("regime")
                _g = _market_state.get("gex_bias")
            context_line = ""
            if _r:
                context_line += "Regime: {}".format(_r.get("regime", "?"))
                if _r.get("vix"):
                    context_line += " (VIX {})".format(_r["vix"])
            if _g and _g.get("gex_b") is not None:
                if context_line:
                    context_line += " | "
                context_line += "GEX: ${}B {}".format(
                    _g["gex_b"], _g.get("tape_bias", ""))

            # Earnings warning if present
            earn_warning = ""
            if sig.get("earnings_warning"):
                earn_warning = "\n\n⚠️ {}".format(sig["earnings_warning"])

            msg = (
                "{sig_type} {horizon} SIGNAL — {grade} ({gpts}pts)\n\n"
                "{sym} {dirn}  •  ${price}\n"
                "Strike: {strike}  •  Premium: ${prem}\n"
                "Stop ${stop}  •  Target ${target}  •  conv x{conv:.2f}\n\n"
                "Gap: {gap}%  •  RS vs SPY: {rs}%\n"
                "Vol: {vol_lbl}{ctx}{ew}"
            ).format(
                sig_type   = sig.get("signal_type", "ORB"),
                horizon    = sig.get("horizon", "INTRADAY"),
                grade      = sig.get("grade", "?"),
                gpts       = sig.get("grade_pts", "?"),
                sym        = sig["symbol"],
                dirn       = sig["direction"],
                price      = sig["price"],
                strike     = sig["strike"],
                prem       = sig["premium"],
                stop       = sig["stop"],
                target     = sig["target"],
                conv       = sig.get("conviction", 1.0) or 1.0,
                gap        = sig.get("gap_pct", "?"),
                rs         = sig.get("rs", "?"),
                vol_lbl    = sig.get("vol_ratio") or "?",
                ctx        = "\n\n" + context_line if context_line else "",
                ew         = earn_warning,
            )
            send_telegram(msg)
            # Auto-open the tracked position so this tier is now locked until
            # the underlying hits T1 (win) or stop (loss).
            try:
                open_auto_position(sig)
            except Exception as _e:
                log("open_auto_position error: {}".format(_e))
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

    log_event("scan.done",
              signal=len(signals),
              watching=len(watching),
              other=len(results) - len(signals) - len(watching),
              total=len(results))


# =============================================
# AI IMPROVEMENT ENGINE
# =============================================

_ai_last_run_date  = ""   # tracks last calendar day AI ran
_ai_last_trade_cnt = 0    # tracks trade count at last AI run

def _time_bucket(h):
    if h < 10:   return "9:30-10:00"
    elif h < 11: return "10:00-11:00"
    elif h < 12: return "11:00-12:00"
    elif h < 13: return "12:00-1:00"
    elif h < 14: return "1:00-2:00"
    else:        return "2:00+"


def _wl_groups(trades, key_fn):
    """{group: {'wins','losses','trades','win_rate'}} -- pure W/L, no $."""
    g = {}
    for t in trades:
        k = key_fn(t)
        if k not in g:
            g[k] = {"wins": 0, "losses": 0}
        if t["outcome"] == "WIN":
            g[k]["wins"] += 1
        else:
            g[k]["losses"] += 1
    for k, v in g.items():
        n = v["wins"] + v["losses"]
        v["trades"]   = n
        v["win_rate"] = round(v["wins"] / n * 100, 1) if n else 0
    return g


def _build_stats_summary(trades):
    """
    Compact W/L stats for the AI prompt. Deliberately contains NO dollar
    P&L -- only outcomes (WIN/LOSS) and R-multiple (did price hit the
    target before the stop). Raw wins/losses per group are exposed so the
    safety gate can sample-test every dimension the AI may tune.
    """
    if not trades:
        return {}

    total = len(trades)
    wins  = sum(1 for t in trades if t["outcome"] == "WIN")
    wr    = round(wins / total * 100, 1) if total else 0
    avg_r = round(sum(t["r_mult"] or 0 for t in trades) / total, 2) if total else 0

    return {
        "total_trades":  total,
        "win_rate":      wr,
        "avg_r_mult":    avg_r,
        "by_symbol":     _wl_groups(trades, lambda t: str(t.get("symbol") or "?")),
        "by_grade":      _wl_groups(trades, lambda t: str(t.get("grade") or "?")),
        "by_direction":  _wl_groups(trades, lambda t: str(t.get("direction") or "?")),
        "by_gap_dir":    _wl_groups(trades, lambda t: str(t.get("gap_dir") or "?")),
        "by_time":       _wl_groups(trades, lambda t: _time_bucket(t.get("entry_hour") or 9.5)),
        "by_signal":     _wl_groups(trades, lambda t: str(t.get("signal_type") or "?")),
        "by_rs":         _wl_groups(trades, lambda t: "rs_positive" if (t.get("rs") or 0) > 0 else "rs_negative"),
        "by_alignment":  _wl_groups(trades, lambda t: "aligned" if t.get("aligned", True) else "counter_trend"),
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
    # Nothing is tuned on a thin sample. The safety gate also enforces a
    # per-group minimum, but this is the cheap global cutoff that stops a
    # handful of trades from triggering a run at all.
    if len(trades) < AI_MIN_TOTAL_SAMPLES:
        log("AI: only {} post-epoch closed trades (need {}) - skipping".format(
            len(trades), AI_MIN_TOTAL_SAMPLES))
        return

    log("AI: Starting improvement run ({} trades, trigger={})".format(len(trades), trigger))

    stats   = _build_stats_summary(trades)
    cfg     = get_config()

    # Remove non-tunable keys from what we send
    cfg_tunable = {k: v for k, v in cfg.items()
                   if k not in ("ai_insight", "ai_focus", "updated_at",
                                 "updated_by", "learning_epoch")}

    prompt = """You are an expert quantitative trader and algorithm optimizer.
You are analyzing a 0DTE (zero days to expiration) options day trading scanner.
Your ONLY goal is to maximize the scanner's WIN RATE -- the fraction of
suggested trades whose price target was hit before the protective stop.

## What you are optimizing on
You are given ONLY win/loss outcomes and avg R-multiple (R = whether and
how far price reached the target versus the stop). This is a pure
"was the suggestion correct" signal.

Dollar profit and loss is intentionally NOT provided and MUST NOT factor
into any decision. Do not infer, estimate, or reason about position size,
premium, capital, or dollar P&L. A high-win-rate group is better than a
low-win-rate group regardless of any imagined dollar amount. If you
mention money in your reasoning the analysis is invalid.

## Current Scanner Config
```json
{config}
```

## Trade Statistics (pure win/loss; counts are real trade counts)
```json
{stats}
```

## Your Task
Return config changes that should raise the win rate of correct calls.
Consider, ONLY where the relevant group has enough trades:
1. Grade thresholds vs actual win rate by grade (by_grade).
2. Factor weights vs which factors separate winners from losers.
3. Whether counter-trend signals should be filtered (by_alignment).
4. Whether the late_entry_hour cutoff matches by_time win rates.
5. Whether vwap_reclaim should stay enabled (by_signal).

## Rules
- A group needs >= {minsamp} trades before you may change a parameter
  driven by it. If a group is below that, leave its parameters UNCHANGED.
- Never change a weight or rank value by more than 3 in one update.
- Never change a grade threshold by more than 3 in one update.
- Grade thresholds: grade_a_min >= 65, must keep a_min > b_min > c_min,
  c_min >= 25.
- Factor weights (weight_*) must each stay within 5..40 and sum to ~100.
- Stay close to a sane baseline; do not chase small win-rate noise.
- If data is insufficient for a dimension, leave that parameter unchanged.
  Returning the current value unchanged is the correct, expected answer
  when evidence is weak.

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
        stats=json.dumps(stats, indent=2),
        minsamp=AI_MIN_TOTAL_SAMPLES,
    )

    try:
        # End-of-day call gets Opus for richer reasoning over the full
        # day's signal set; intraday updates stay on Sonnet for speed/cost.
        if trigger == "end_of_day":
            ai_model     = "claude-opus-4-7"
            ai_max_tokens = 1500
        else:
            ai_model     = "claude-sonnet-4-20250514"
            ai_max_tokens = 1000

        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      ai_model,
                "max_tokens": ai_max_tokens,
                "messages":   [{"role": "user", "content": prompt}]
            },
            timeout=60
        )
        log_event("anthropic.response", source="ai_loop",
                  status=resp.status_code, model=ai_model)

        if resp.status_code != 200:
            log_event("anthropic.error", level="error", source="ai_loop",
                      status=resp.status_code, model=ai_model,
                      body=resp.text[:200])
            # Backoff on auth/billing errors — these don't fix themselves
            # within minutes, so stop retrying until tomorrow.
            err_text = resp.text.lower()
            if resp.status_code in (401, 402, 403) or \
               "credit balance" in err_text or \
               "invalid api key" in err_text or \
               "rate limit" in err_text and resp.status_code == 429:
                et = pytz.timezone("America/New_York")
                _ai_last_run_date = datetime.now(et).strftime("%Y-%m-%d")
                _ai_last_trade_cnt = len(trades)
                log_warn("AI: backoff engaged — will not retry until tomorrow")
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

        # --- Sample-size gate — every tuned key needs >= AI_MIN_TOTAL_SAMPLES
        # supporting trades in its driving group, is delta-clipped, and is
        # anchored so it cannot drift unboundedly from DEFAULT_CONFIG. ---
        if HAS_SAFETY_GATES:
            try:
                def _wl(group_map, name):
                    g = (stats.get(group_map) or {}).get(name, {})
                    return {"wins": g.get("wins", 0), "losses": g.get("losses", 0)}

                # Map each tunable key to the trade group that justifies it.
                group_stats = {
                    "counter_trend": _wl("by_alignment", "counter_trend"),
                    "vwap_reclaim":  _wl("by_signal", "vwap_reclaim"),
                    "grade_a":       _wl("by_grade", "A"),
                    "grade_b":       _wl("by_grade", "B"),
                    "grade_c":       _wl("by_grade", "C"),
                    "rs":            _wl("by_rs", "rs_positive"),
                    # Global pool for parameters not tied to one sub-group.
                    "global": {
                        "wins":   sum(1 for t in trades if t["outcome"] == "WIN"),
                        "losses": sum(1 for t in trades if t["outcome"] != "WIN"),
                    },
                }
                safe_updates, rejected = safety_gates.filter_ai_proposed_changes(
                    current_cfg  = cfg_tunable,
                    proposed_cfg = updates,
                    group_stats  = group_stats,
                    default_cfg  = DEFAULT_CONFIG,
                    min_samples  = AI_MIN_TOTAL_SAMPLES,
                )
                if rejected:
                    log("AI: safety gate rejected/clipped {} changes:".format(len(rejected)))
                    for r in rejected:
                        log("AI:   {} -> {}".format(r.get("key"), r.get("reason")))
                updates = {k: safe_updates[k] for k in updates if k in safe_updates}
            except Exception as e:
                log("AI: safety gate error (rejecting all changes this run): {}".format(e))
                updates = {}

        # Enforce structural invariants in code, regardless of what the AI
        # or the gate produced (weights in-band + ~sum 100, grade ordering
        # and floors). Prompt-stated rules are not trusted at face value.
        if HAS_SAFETY_GATES and updates:
            try:
                merged = dict(cfg_tunable)
                merged.update(updates)
                merged = safety_gates.enforce_config_invariants(merged)
                updates = {k: merged[k] for k in updates if k in merged}
            except Exception as e:
                log("AI: invariant enforcement error (rejecting changes): {}".format(e))
                updates = {}

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


def _filter_trades_since(trades, cutoff_iso_date):
    """Trades whose ts (ISO) is on or after cutoff_iso_date (YYYY-MM-DD)."""
    out = []
    for t in trades:
        ts = t.get("ts") or ""
        if ts[:10] >= cutoff_iso_date:
            out.append(t)
    return out


def run_friday_digest():
    """
    Friday-only weekly upgrade summary, sent via Telegram.
    Uses Opus 4.7 for synthesis. Runs once per Friday after close.
    """
    if not ANTHROPIC_KEY:
        log("FridayDigest: ANTHROPIC_API_KEY not set - skipping")
        return

    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    if now.weekday() != 4:   # 0=Mon..4=Fri
        log("FridayDigest: not Friday - skipping")
        return

    all_trades = db_get_all_closed_trades()
    if len(all_trades) < 5:
        log("FridayDigest: only {} closed trades - skipping".format(len(all_trades)))
        return

    # Last 7 calendar days (covers the 5-trading-day week)
    cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    week_trades = _filter_trades_since(all_trades, cutoff)
    if len(week_trades) < 3:
        log("FridayDigest: only {} trades this week - skipping".format(len(week_trades)))
        return

    stats    = _build_stats_summary(week_trades)
    cfg      = get_config()
    analyses = db_get_ai_analyses(limit=5)
    proposals = db_get_proposals(status="pending", limit=5) if "db_get_proposals" in globals() else []

    recent_insights = [{
        "date":    (a.get("ts") or "")[:10],
        "insight": a.get("insight") or "",
        "focus":   a.get("focus") or "",
    } for a in analyses]

    proposal_briefs = [{
        "title":   p.get("title", ""),
        "summary": p.get("summary", ""),
    } for p in proposals]

    prompt = """You are reviewing a week of automated 0DTE/swing options signals from an algorithmic trading system. \
Most outcomes are paper-traded (synthetic backtest of the system's own signals against intraday bars), so treat them as signal quality rather than realized PnL.

WEEK STATS:
{stats}

RECENT AI INSIGHTS (newest first):
{insights}

PENDING STRUCTURAL PROPOSALS:
{proposals}

CURRENT FOCUS: {focus}

Write a Telegram message under 380 words. Plain text only (no markdown, no asterisks, no backticks). \
Use these sections, each prefixed exactly as written:

WEEK RECAP:
2 sentences. Overall signal quality this week, citing win-rate and avg-R.

TOP UPGRADES (next week):
Numbered list of 3 specific, actionable changes. Each: one short title line, then 1-2 sentences of reasoning that cite specific stats (which symbol/grade/time bucket/regime). \
Prefer config tweaks the AI loop can already make; only suggest structural changes if data clearly warrants new code.

WATCH:
1-2 sentences on the single biggest risk or unknown to monitor next week.

Be direct. No "consider" / "you might want to". No emojis. No closing pleasantries.""".format(
        stats     = json.dumps(stats,           indent=2),
        insights  = json.dumps(recent_insights, indent=2),
        proposals = json.dumps(proposal_briefs, indent=2) if proposal_briefs else "(none)",
        focus     = cfg.get("ai_focus", "(none)"),
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
                "model":      "claude-opus-4-7",
                "max_tokens": 1500,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=90,
        )
        log_event("anthropic.response", source="friday_digest",
                  status=resp.status_code)
        if resp.status_code != 200:
            log_event("anthropic.error", level="error", source="friday_digest",
                      status=resp.status_code, body=resp.text[:200])
            return
        body = resp.json()["content"][0]["text"].strip()
    except Exception as e:
        log_event("anthropic.exception", level="error",
                  source="friday_digest", error=str(e))
        return

    header  = "Weekly Upgrade Digest - {}".format(now.strftime("%Y-%m-%d"))
    summary = "Trades analyzed: {} | Win rate: {}% | Avg R: {}".format(
        stats.get("total_trades", 0),
        stats.get("win_rate", 0),
        stats.get("avg_r_mult", 0),
    )
    msg = "{}\n{}\n\n{}".format(header, summary, body)

    if send_telegram(msg):
        log("FridayDigest: sent ({} chars)".format(len(msg)))
    else:
        log("FridayDigest: send_telegram failed")


def _run_proposal_analysis(trades, stats):
    """
    Second AI pass: looks for structural improvements the config system
    cannot handle -- new strategies, indicators, signal types.
    Saves proposals to DB and sends top one via Telegram.
    """
    if not ANTHROPIC_KEY or len(trades) < 5:
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
            log_event("anthropic.error", level="error",
                      source="ai_proposals", status=resp.status_code,
                      body=resp.text[:200])
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
    # FIX 3: raised threshold 0.85 -> 0.92 (was rejecting valid bases with only 8-14%
    # ATR contraction; volatile markets often can't clear the original 15% bar)
    if atr_prior > 0 and atr_base / atr_prior > 0.92:
        return None   # ATR not contracted enough -- not a tight base

    # 3. Breakout above base high (pivot) -- check today or last 7 days
    # FIX 4: extended lookback range(0,4) -> range(0,8) so we catch breakouts
    # up to 7 days old that are still holding above the pivot (valid continuation entries)
    today = daily[-1]
    pivot_broken = False
    days_since_breakout = 0

    for lookback_days in range(0, 8):
        bar = daily[-(1 + lookback_days)]
        vol_ratio = bar["v"] / avg_vol_50 if avg_vol_50 > 0 else 0
        if bar["h"] > base_high and bar["c"] > base_high * 0.995:
            if vol_ratio >= 1.40:   # 40%+ above average (O'Neil requirement)
                pivot_broken        = True
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

    # 5. FIX 2: Weinstein Stage 1 or 2 filter (was Stage 2 only -- Stage 1 base
    # breakouts are valid setups and were being rejected across the board)
    stage, sma_rising = weinstein_stage(daily, weekly)
    if stage not in (1, 2):
        return None

    # Probability calculation (O'Neil-calibrated)
    prob = 62
    if breakout_vol_ratio >= 2.0:   prob += 8    # strong institutional conviction
    elif breakout_vol_ratio >= 1.6: prob += 4
    if days_since_breakout == 0:    prob += 5    # fresh breakout today
    if close_rank >= 0.80:          prob += 4    # closed at top of range
    if atr_base / (atr_prior + 0.001) < 0.60:   prob += 3   # very tight base
    if stage == 1:                  prob -= 4    # slight haircut vs confirmed Stage 2
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
        "notes":         "Base: {:.0f}wks | Pivot: ${:.2f} | Vol: {:.1f}x | Close rank: {:.0f}% | Stage {}".format(
                            base_weeks, base_high, breakout_vol_ratio, close_rank * 100, stage),
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
    # FIX 6: lowered recovery vol threshold from 0.9x to 0.75x avg_vol
    # Large-caps and ETFs rarely show dramatic recovery volume; 0.75x is still
    # meaningful confirmation that sellers are not in control
    recent_bars    = daily[spring_idx + 1:]
    if recent_bars:
        recovery_vol   = statistics.mean([b["v"] for b in recent_bars])
        vol_expanding  = recovery_vol > avg_vol * 0.75
    else:
        vol_expanding  = False

    # Weinstein Stage: spring should occur in Stage 1/2 (accumulation zone)
    # FIX 2: changed from stage==4 exclusion to requiring stage in (1,2)
    # Wyckoff springs by definition happen during accumulation (Stage 1) or
    # early markup (Stage 2) -- Stage 3/4 springs are bear-market traps
    stage, _ = weinstein_stage(daily, weekly)
    if stage not in (1, 2):
        return None

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

    # 2. FIX 5: Tight consolidation relaxed from 15 bars/90% to 10 bars/88%
    # Original requirement was too strict: many valid setups form bases with
    # slightly wider ranges or fewer bars, especially in volatile markets
    tight_bars = [b for b in daily[-50:] if b["c"] >= high_all * 0.88]
    if len(tight_bars) < 10:
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

    # FIX 2: Accept Stage 1 and Stage 2 for CALL continuations (was Stage 2 only)
    # Earnings gaps in Stage 1 can be valid re-ratings from a basing phase;
    # only Stage 3 (topping) and Stage 4 (decline) should be excluded
    stage, _ = weinstein_stage(daily, None)
    if direction == "CALL" and stage not in (1, 2):
        return None

    # Probability (O'Neil / Zacks calibrated)
    prob  = 60
    prob += max(0, 12 - days_since)
    prob += 8 if pullback < 0.15 else 0
    prob += 5 if earn_gap > 6.0  else 0
    if stage == 1: prob -= 3   # slight haircut for Stage 1 vs Stage 2
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

def _check_earnings_iv_crush(symbol):
    """
    Detect the earnings IV-crush setup. Returns a swing-signal-shaped dict
    or None.

    Conditions (ALL must hold):
      - Tomorrow is the symbol's next earnings date
      - IV Rank >= 70 (premium is rich vs the stock's own history)
      - At least 30 days of IV history stored
      - Implied move > historical avg post-earnings move by 1pp+
      - At least 4 prior earnings observations
    """
    if not (HAS_NEW_STRATS and HAS_IV_RANK and HAS_SAFETY_GATES):
        return None

    try:
        dte = safety_gates.days_to_earnings(symbol)
        if dte != 1:
            return None

        # Need IV Rank
        spot = get_current_price(symbol)
        if not spot:
            return None

        iv_data = iv_rank.compute_iv_rank(symbol)
        if not iv_data or iv_data["iv_rank"] < 70:
            return None
        # Variance-risk-premium gate: don't sell premium when implied vol is
        # not at least 20% richer than realized. iv_rv_ratio is added by
        # compute_iv_rank when realized vol is computable; if absent we let
        # the trade through (back-compat for symbols without RV history).
        if iv_data.get("iv_rv_ratio") is not None and not iv_data.get("vrp_favorable"):
            return None

        # Need ATM straddle pricing
        call_premium, _ck, _cl, _cd = get_liquid_option(symbol, "CALL", spot)
        put_premium,  _pk, _pl, _pd = get_liquid_option(symbol, "PUT",  spot)
        if not call_premium or not put_premium:
            return None

        daily = get_daily_extended(symbol, limit=400)
        prior_earnings = safety_gates.get_prior_earnings_dates(symbol)
        if not daily or len(prior_earnings) < 4:
            return None

        sig = new_strategies.detect_earnings_iv_crush(
            symbol               = symbol,
            spot                 = spot,
            atm_call             = call_premium,
            atm_put              = put_premium,
            iv_rank              = iv_data["iv_rank"],
            daily_bars           = daily,
            prior_earnings_dates = prior_earnings,
            days_to_earnings     = dte,
        )
        if not sig:
            return None

        # Adapt to the swing-signal shape used elsewhere
        return {
            "signal_type":   "EARNINGS_IV_CRUSH",
            "signal_label":  "Earnings IV Crush (short premium)",
            "direction":     "SHORT_PREMIUM",
            "structure":     sig["structure"],
            "tier":          1,
            "prob":          int(sig["expected_win_rate"] * 100),
            "score":         sig["score"],
            "spot":          spot,
            "iv_rank":       iv_data["iv_rank"],
            "iv_percentile": iv_data["iv_percentile"],
            "implied_move":  sig["implied_move"],
            "historic_move": sig["historical_move"],
            "edge_pct":      sig["edge_pct"],
            "short_call_k":  sig["short_call_k"],
            "long_call_k":   sig["long_call_k"],
            "short_put_k":   sig["short_put_k"],
            "long_put_k":    sig["long_put_k"],
            "exit_when":     sig["exit_when"],
            "symbol":        symbol,
        }
    except Exception as e:
        log("IV crush check error for {}: {}".format(symbol, e))
        return None


_market_trend_cache = {"ts": 0, "trend": None}


def market_trend():
    """Broad-tape trend for the weekly directional gate: 'UP'/'DOWN'/'FLAT'.

    SPY close vs its 50-day SMA, with the SMA slope as a tie-breaker. Cached
    for an hour so the per-symbol weekly sweep doesn't recompute it 70x.
    """
    now = time.time()
    if _market_trend_cache["trend"] and now - _market_trend_cache["ts"] < 3600:
        return _market_trend_cache["trend"]
    trend = "FLAT"
    try:
        spy = get_daily_extended("SPY", limit=80) or []
        if len(spy) >= 55:
            closes = [b["c"] for b in spy]
            sma50_now  = sum(closes[-50:]) / 50.0
            sma50_prev = sum(closes[-55:-5]) / 50.0
            price = closes[-1]
            if price > sma50_now and sma50_now >= sma50_prev:
                trend = "UP"
            elif price < sma50_now and sma50_now <= sma50_prev:
                trend = "DOWN"
            else:
                trend = "FLAT"
    except Exception:
        pass
    _market_trend_cache.update({"ts": now, "trend": trend})
    return trend


def scan_swing_symbol(symbol):
    """
    Tier 1: fully-triggered signals (O'Neil, Wyckoff Spring, 52W, Earnings Cont.)
    Tier 2: pattern-forming watchlist (base building, accum zone, near high, gap holding)
    Tier 3 (NEW): Earnings IV crush — short premium 1 day before earnings
    """
    try:
        # --- Tier 3: Earnings IV Crush (runs BEFORE the blackout filter) ---
        # On the day before earnings, IF iv_rank > 70 AND implied move > historical,
        # fire a short-premium iron condor setup. This is the ONE case where we
        # WANT to trade into earnings.
        iv_crush_sig = _check_earnings_iv_crush(symbol)
        if iv_crush_sig:
            _swing_stats_bump("iv_crush")
            return iv_crush_sig

        # --- Earnings blackout: skip directional swing trades within 10 trading days ---
        if HAS_SAFETY_GATES:
            try:
                allowed, reason = safety_gates.earnings_filter(symbol, "swing")
                if not allowed:
                    _swing_stats_bump("earnings_blackout")
                    log("swing {}: skipped — {}".format(symbol, reason))
                    return None
            except Exception as e:
                log("earnings_filter error for {}: {}".format(symbol, e))

        daily  = get_daily_extended(symbol, limit=260)
        weekly = get_weekly_bars(symbol, limit=52)
        if not daily or len(daily) < 60:
            _swing_stats_bump("no_data")
            return None
        current = daily[-1]["c"]
        if not current or current <= 0:
            _swing_stats_bump("no_data")
            return None
        _swing_stats_bump("had_data")

        spy_d  = get_daily_extended("SPY", limit=10)
        spy_rs = 0.0
        if spy_d and len(spy_d) >= 5:
            spy_chg = (spy_d[-1]["c"] - spy_d[-5]["c"]) / spy_d[-5]["c"] * 100
            sym_chg = (daily[-1]["c"]  - daily[-5]["c"]) / daily[-5]["c"] * 100
            spy_rs  = round(sym_chg - spy_chg, 2)

        type_pri = {"ONEIL_PIVOT": 4, "WYCKOFF_SPRING": 3, "HI52_BREAKOUT": 2, "EARNINGS_CONT": 1}

        tier1_sigs = []
        for name, fn in [("oneil",   lambda: detect_oneil_pivot(daily, weekly)),
                          ("wyckoff", lambda: detect_wyckoff_spring(daily, weekly)),
                          ("hi52",    lambda: detect_52w_breakout(daily, weekly)),
                          ("earn",    lambda: detect_earnings_continuation(daily, weekly))]:
            try:
                r = fn()
                if r:
                    r["tier"] = 1
                    tier1_sigs.append(r)
                    _swing_stats_bump("t1_" + name)
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
            _swing_stats_bump("no_signal")
            return None
        if tier1_sigs:
            _swing_stats_bump("tier1")
        else:
            _swing_stats_bump("tier2_only")

        if tier1_sigs:
            best = max(tier1_sigs, key=lambda s: (s["prob"], type_pri.get(s["signal_type"], 0)))
        else:
            best = max(tier2_sigs, key=lambda s: s["prob"])

        direction = best["direction"]
        tier      = best["tier"]

        # Weekly quality gates (tier-1 only). A failed gate downgrades the
        # setup to tier-2 -- it stays visible on the dashboard as a watch, but
        # won't alert or auto-open a position (both are tier-1 only).
        if tier == 1:
            wcfg        = get_config()
            min_rs      = wcfg.get("weekly_min_rs", 0.0)
            max_age     = wcfg.get("weekly_max_breakout_age", 3)
            gate_reason = None

            # 1) Trade with the broad tape: long setups need positive RS and a
            #    non-falling market; PUT (earnings) setups need the inverse.
            if wcfg.get("weekly_require_uptrend", True):
                mt = market_trend()
                if direction == "CALL" and (spy_rs < min_rs or mt == "DOWN"):
                    gate_reason = "counter-trend CALL (RS {:.1f}, tape {})".format(
                        spy_rs, mt)
                elif direction == "PUT" and (spy_rs > -min_rs or mt == "UP"):
                    gate_reason = "counter-trend PUT (RS {:.1f}, tape {})".format(
                        spy_rs, mt)

            # 2) Don't chase a breakout that already ran days ago (breakout
            #    detectors only -- earnings 'days_since' means days-since-report).
            if gate_reason is None and best.get("signal_type") in (
                    "ONEIL_PIVOT", "WYCKOFF_SPRING", "HI52_BREAKOUT"):
                age = best.get("days_since")
                if age is not None and age > max_age:
                    gate_reason = "stale breakout ({}d old)".format(age)

            # 3) Earnings continuation from a Stage-1 base is a weak entry.
            if (gate_reason is None
                    and best.get("signal_type") == "EARNINGS_CONT"
                    and best.get("stage") == 1):
                gate_reason = "stage-1 earnings continuation"

            if gate_reason:
                tier = 2
                best["tier"] = 2
                best["gate_reason"] = gate_reason
                _swing_stats_bump("tier1_gated")
                log("{}: tier-1 downgraded -> {}".format(symbol, gate_reason))

        exts = best.get("extend", {})
        if direction == "CALL":
            above = sorted([(r, v) for r, v in exts.items() if v > current], key=lambda x: x[1])
        else:
            above = sorted([(r, v) for r, v in exts.items() if v < current],
                           key=lambda x: x[1], reverse=True)

        # Fib-extension targets (the legacy basis, also fed to the blend below).
        t1 = above[0][1] if len(above) >= 1 else None
        t2 = above[1][1] if len(above) >= 2 else None
        t3 = above[2][1] if len(above) >= 3 else None

        stop = (best.get("retrace", {}).get(0.618)
                or round(current * (0.96 if direction == "CALL" else 1.04), 2))

        # --- Weekly expected-move / Fibonacci blend (current-week settlement) ---
        week_expiry, dte = current_week_expiry(
            zero_dte_cutoff=get_config().get("zero_dte_cutoff_hour", 14.5))
        atm_iv = expected_move = None
        target_basis = "FIB"
        t1_prob = t2_prob = None
        if HAS_KEY_LEVELS:
            try:
                kl = key_levels_mod.get_key_levels(
                    symbol, daily_bars=daily, spot=current,
                    direction=direction, week_expiry=week_expiry, dte=dte)
                atm_iv        = kl.atm_iv
                expected_move = kl.expected_move_1sd
            except Exception:
                pass
        if HAS_TARGETS:
            try:
                tg = targets_mod.compute_price_targets(
                    current, direction, "WEEKLY", iv=atm_iv, dte=dte,
                    fib_extend=best.get("extend", {}),
                    fib_retrace=best.get("retrace", {}))
                if tg:
                    if tg.get("t1") is not None:
                        t1 = tg["t1"]
                    if tg.get("t2") is not None:
                        t2 = tg["t2"]
                    if tg.get("stop") is not None:
                        stop = tg["stop"]
                    t1_prob       = tg.get("t1_prob")
                    t2_prob       = tg.get("t2_prob")
                    target_basis  = tg.get("basis", target_basis)
                    expected_move = tg.get("expected_move_1sd", expected_move)
            except Exception:
                pass

        rr1  = round(abs(t1 - current) / abs(current - stop), 2) if (
            t1 and stop and abs(current - stop) > 0) else 0

        # Weekly option on the current-week settlement (tier-1 only). Build a
        # dict the dashboard/renderers expect; premium-stop/target are
        # size-free per-contract levels.
        opt = None
        opt_volume = None
        opt_strike = None
        if tier == 1:
            prem, opt_strike, is_live, dte_label, opt_volume = get_swing_option(
                symbol, direction, current)
            if prem and is_live:
                opt = {
                    "premium": prem,
                    "strike":  opt_strike,
                    "expiry":  week_expiry,
                    "dte":     dte,
                    "delta":   0.40,
                    "iv":      round(atm_iv * 100, 1) if atm_iv else 0,
                    "volume":  opt_volume,
                    "bid":     "-",
                    "ask":     "-",
                }

        product_class = "ETF" if symbol in ETF_PRODUCTS else "STOCK"

        # Volume-vs-OI confluence on the weekly contract (Alpaca volume x
        # Databento OI). SPY/QQQ already have OI from the daily GEX pipeline;
        # for tier-1 stock setups allow an on-demand cached OI sweep.
        vol_oi = None
        if HAS_VOL_OI and tier == 1 and opt_strike is not None:
            try:
                vol_oi = vol_oi_mod.compute_vol_oi(
                    symbol, opt_strike, week_expiry,
                    "call" if direction == "CALL" else "put",
                    alpaca_volume=opt_volume,
                    allow_fetch=(product_class == "STOCK"))
            except Exception as e:
                log("vol_oi error {}: {}".format(symbol, e))

        # Map directional targets onto the und_call_*/und_put_* keys so
        # db_log_signal and the paper trader can persist them.
        und_keys = {}
        if direction == "CALL":
            und_keys = {"und_call_t1": t1, "und_call_t2": t2, "und_call_stop": stop}
        else:
            und_keys = {"und_put_t1": t1, "und_put_t2": t2, "und_put_stop": stop}

        # Higher-than-expected volume vs OI lifts continuation probability as an
        # added confluence factor (only when both feeds confirmed it).
        prob = best["prob"]
        if vol_oi and vol_oi.get("points"):
            prob = min(95, prob + vol_oi["points"])

        sig = {
            "symbol":         symbol,
            "price":          round(current, 2),
            "direction":      direction,
            "tier":           tier,
            "horizon":        "WEEKLY",
            "product_class":  product_class,
            "conviction":     current_conviction(),
            "signal_type":    best["signal_type"],
            "signal_label":   best["signal_label"],
            "prob":           prob,
            "base_prob":      best["prob"],
            "vol_oi":         vol_oi,
            "spy_rs":         spy_rs,
            "rs":             spy_rs,
            "t1":             t1,
            "t2":             t2,
            "t3":             t3,
            "stop":           round(stop, 2) if stop is not None else None,
            "t1_prob":        t1_prob,
            "t2_prob":        t2_prob,
            "target_basis":   target_basis,
            "expected_move":  expected_move,
            "atm_iv":         atm_iv,
            "week_expiry":    week_expiry,
            "dte":            dte,
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
        sig.update(und_keys)
        if HAS_SCANNER_CORE:
            try:
                sig["rationale"] = scanner_core.build_rationale(sig)
            except Exception:
                pass
        return sig
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

_swing_stats           = {}
_swing_stats_lock      = threading.Lock()
_swing_fetch_err_count = 0  # rate-limit per-symbol fetch error logging


def _swing_stats_bump(key):
    with _swing_stats_lock:
        _swing_stats[key] = _swing_stats.get(key, 0) + 1


def _swing_stats_reset():
    global _swing_fetch_err_count
    with _swing_stats_lock:
        _swing_stats.clear()
        _swing_fetch_err_count = 0


def _swing_stats_snapshot():
    with _swing_stats_lock:
        return dict(_swing_stats)


def _log_swing_fetch_err(symbol, kind, detail):
    """
    Surface per-symbol Alpaca data-fetch failures with throttling.
    Without this the swing scan can report "data:0/72" while bare excepts
    swallow the actual cause (auth, rate limit, status code). Cap at 5
    lines per scan so a full-universe outage doesn't flood the log.
    """
    global _swing_fetch_err_count
    with _swing_stats_lock:
        if _swing_fetch_err_count >= 5:
            return
        _swing_fetch_err_count += 1
    log("  swing fetch fail [{}] {}: {}".format(kind, symbol, detail))


def _send_weekly_alert(sig):
    """Telegram alert for a weekly tier-1 setup (current-week settlement)."""
    opt = sig.get("option") or {}
    rationale = sig.get("rationale") or {}
    t1, t2 = sig.get("t1"), sig.get("t2")
    t1p = sig.get("t1_prob")
    msg = (
        "{stype} WEEKLY — {sym} {dirn}  •  ${price}\n"
        "Exp {exp} ({dte}DTE)  •  conv x{conv:.2f}\n"
        "T1 {t1}{t1p}  •  T2 {t2}  •  Stop {stop}  •  basis {basis}\n"
        "RS {rs} vs SPY  •  prob {prob}%{prem}{summ}"
    ).format(
        stype = sig.get("signal_type", "SWING"),
        sym   = sig.get("symbol"),
        dirn  = sig.get("direction"),
        price = sig.get("price"),
        exp   = sig.get("week_expiry", "?"),
        dte   = sig.get("dte", "?"),
        conv  = sig.get("conviction", 1.0) or 1.0,
        t1    = t1 if t1 is not None else "-",
        t1p   = " ({}%)".format(t1p) if t1p is not None else "",
        t2    = t2 if t2 is not None else "-",
        stop  = sig.get("stop", "-"),
        basis = sig.get("target_basis", "FIB"),
        rs    = sig.get("rs", sig.get("spy_rs", "?")),
        prob  = sig.get("prob", "?"),
        prem  = ("\nPremium ${} strike {}".format(opt["premium"], opt.get("strike"))
                 if opt.get("premium") else ""),
        summ  = ("\n" + rationale["summary"]) if rationale.get("summary") else "",
    )
    send_telegram(msg)


def run_unified_scan(do_intraday=False, do_weekly=False):
    """Assemble the unified tiered payload from current scanner state.

    The intraday (scan_all_symbols/run_signal_scan) and weekly (run_swing_scan)
    passes run on their own cadences from the scheduler; this is the single
    read-side entry point that merges their latest results into one tiered,
    conviction-ranked payload plus SPX/NDX context. Optionally (re)triggers a
    pass first when do_intraday / do_weekly is set.
    """
    if do_weekly:
        run_swing_scan()
    if do_intraday:
        run_signal_scan()

    with state_lock:
        intraday = [r for r in all_signals
                    if r.get("status") in ("SIGNAL", "SIGNAL (no options)")]
        weekly   = list(swing_signals)

    context = {"index": index_context()}
    if HAS_SCANNER_CORE:
        return scanner_core.merge_and_rank(intraday, weekly, context)
    return {"weekly": weekly, "intraday": intraday, "context": context}


def run_swing_scan():
    """
    Scan SWING_SYMBOLS concurrently (batches of 8).
    Updates both swing_signals (used by renderer) and all_swing_signals (used by chat).
    """
    global swing_signals, next_swing_scan, all_swing_signals, swing_next_scan_at
    # Daily bars don't change overnight or on weekends/holidays, so a
    # 72-symbol Alpaca sweep outside the session is guaranteed-empty
    # waste. Gate on trading-day + a window that covers premarket + RTH
    # + the post-close print (~9:00 AM -> 4:15 PM ET).
    et       = pytz.timezone("America/New_York")
    et_hour  = datetime.now(et).hour + datetime.now(et).minute / 60.0
    if not (is_trading_day() and 9.0 <= et_hour <= 16.25):
        next_swing_scan    = time.time() + SWING_SCAN_INTERVAL
        swing_next_scan_at = next_swing_scan
        return
    _swing_stats_reset()
    log("=== Swing scan starting ({} symbols) ===".format(len(SWING_SYMBOLS)))

    results      = []
    results_lock = threading.Lock()

    def worker(sym):
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

    # Resolve live auto positions first (may free the WEEKLY tier).
    try:
        monitor_active_positions()
    except Exception as e:
        log("monitor_active_positions (weekly) error: {}".format(e))

    # Weekly alerts: ONE live WEEKLY position at a time (tier lock), and within
    # that, one alert per (symbol, direction, week_expiry) so a setup never
    # re-fires until its Friday settlement rolls. Alert only the single best
    # not-yet-alerted tier-1 setup, then lock the tier until it closes.
    if tier_has_open_position("WEEKLY"):
        log("WEEKLY tier locked -- live position open, suppressing alerts")
    else:
        for sig in results:                         # results sorted by prob desc
            if sig.get("tier") != 1:
                continue
            key = (sig.get("symbol"), sig.get("direction"), sig.get("week_expiry"))
            with _weekly_alert_lock:
                if key in _weekly_alerted:
                    continue
                _weekly_alerted.add(key)
            try:
                db_log_signal(sig)
            except Exception as e:
                log("weekly db_log_signal error: {}".format(e))
            try:
                open_auto_position(sig)
            except Exception as e:
                log("weekly open_auto_position error: {}".format(e))
            if bot_enabled:
                try:
                    _send_weekly_alert(sig)
                except Exception as e:
                    log("weekly alert error: {}".format(e))
            break  # one weekly position at a time

    # Compact filter-stage breakdown -- tells you WHY a 0-setups scan
    # was 0 (no_data vs earnings_blocked vs no_signal) and which tier-1
    # detectors fired. Keep keys short so the line is grep-friendly.
    s = _swing_stats_snapshot()
    parts = [
        "data:{}/{}".format(s.get("had_data", 0), len(SWING_SYMBOLS)),
        "no_data:{}".format(s.get("no_data", 0)),
        "earn_blk:{}".format(s.get("earnings_blackout", 0)),
        "no_sig:{}".format(s.get("no_signal", 0)),
        "t1:{}".format(s.get("tier1", 0)),
        "t2_only:{}".format(s.get("tier2_only", 0)),
        "iv_crush:{}".format(s.get("iv_crush", 0)),
        "det[oneil={} wyckoff={} hi52={} earn={}]".format(
            s.get("t1_oneil", 0), s.get("t1_wyckoff", 0),
            s.get("t1_hi52", 0), s.get("t1_earn", 0)),
    ]
    if s.get("served_stale"):
        parts.append("stale:{}".format(s.get("served_stale")))
    log("Swing scan done: {} setups | {}".format(len(results), " ".join(parts)))

    # A whole-universe data outage is the single most common reason the swing
    # engine silently goes dark. The per-symbol fetch errors are throttled to
    # 5 lines/scan, so without this an outage looks identical to a quiet day.
    # When nobody got data, report the dominant HTTP status so the cause
    # (429 rate-limit vs 401/403 auth vs 5xx) is unambiguous.
    if s.get("had_data", 0) == 0:
        http_counts = {k[5:]: v for k, v in s.items() if k.startswith("http_")}
        if http_counts:
            dominant = max(http_counts.items(), key=lambda kv: kv[1])
            log("Swing universe OUTAGE: 0/{} symbols had data | "
                "dominant status={} ({} hits) | statuses={}".format(
                    len(SWING_SYMBOLS), dominant[0], dominant[1],
                    ", ".join("{}={}".format(k, v) for k, v in
                              sorted(http_counts.items()))))
        else:
            log("Swing universe OUTAGE: 0/{} symbols had data | "
                "no HTTP status captured (check Alpaca auth / connectivity)".format(
                    len(SWING_SYMBOLS)))


def _build_weekly_section():
    """Build the WEEKLY/PRIMARY tier as an embeddable HTML section.

    Returns just the inner section (header + legend + cards), no page wrapper,
    so it can be dropped into the single unified dashboard alongside the
    intraday/0DTE tier. (Formerly render_swing_dashboard, a standalone page.)
    """
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

        # Horizon / product-class / conviction / target-basis badges
        _hz   = s.get("horizon", "WEEKLY")
        _pc   = s.get("product_class", "")
        _conv = s.get("conviction")
        _basis = s.get("target_basis")
        stage_badge += ("<span style='background:#0d1117;color:#58a6ff;font-size:9px;"
                        "font-weight:700;padding:2px 7px;border-radius:3px;"
                        "border:1px solid #1f3d5c;margin-right:4px'>{} {}</span>"
                        ).format(_hz, _pc).replace("  ", " ")
        if _conv is not None:
            _cc = "#3fb950" if _conv >= 1.0 else "#e3b341" if _conv >= 0.7 else "#f85149"
            stage_badge += ("<span style='background:#0d1117;color:{};font-size:9px;"
                            "font-weight:700;padding:2px 7px;border-radius:3px;"
                            "border:1px solid {};margin-right:4px'>conv x{:.2f}</span>"
                            ).format(_cc, _cc, _conv)
        if _basis:
            stage_badge += ("<span style='background:#0d1117;color:#a371f7;font-size:9px;"
                            "font-weight:700;padding:2px 7px;border-radius:3px;"
                            "border:1px solid #2d1b69;margin-right:4px'>{}</span>"
                            ).format(_basis)
        _vo = s.get("vol_oi")
        if _vo and _vo.get("flag") in ("ELEVATED", "UNUSUAL"):
            _voc = "#f0883e" if _vo["flag"] == "UNUSUAL" else "#e3b341"
            stage_badge += ("<span style='background:#0d1117;color:{c};font-size:9px;"
                            "font-weight:700;padding:2px 7px;border-radius:3px;"
                            "border:1px solid {c};margin-right:4px' "
                            "title='Alpaca volume vs Databento OI'>VOL/OI {r} {f}</span>"
                            ).format(c=_voc, r=_vo.get("ratio"), f=_vo["flag"])

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
                        "check broker for this-week {dir} expiry</div>").format(dir=direction)

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
                  letter-spacing:.5px;margin-bottom:3px'>Targets ({basis})</div>
      <div style='font-size:11px;line-height:1.7'>
        <span style='color:#8b949e'>T1 </span>
        <span style='color:#58a6ff;font-family:monospace'>${t1}</span>
        <span style='color:#6e7681'>{t1p}</span><br>
        <span style='color:#8b949e'>T2 </span>
        <span style='color:#a371f7;font-family:monospace'>${t2}</span>
        <span style='color:#6e7681'>{t2p}</span><br>
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
            t1p        = "({}%)".format(s.get("t1_prob")) if s.get("t1_prob") is not None else "",
            t2p        = "({}%)".format(s.get("t2_prob")) if s.get("t2_prob") is not None else "",
            basis      = s.get("target_basis") or "FIB",
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

        # Horizon / product-class / conviction / target-basis badges
        _hz   = s.get("horizon", "WEEKLY")
        _pc   = s.get("product_class", "")
        _conv = s.get("conviction")
        _basis = s.get("target_basis")
        stage_badge += ("<span style='background:#0d1117;color:#58a6ff;font-size:9px;"
                        "font-weight:700;padding:2px 7px;border-radius:3px;"
                        "border:1px solid #1f3d5c;margin-right:4px'>{} {}</span>"
                        ).format(_hz, _pc).replace("  ", " ")
        if _conv is not None:
            _cc = "#3fb950" if _conv >= 1.0 else "#e3b341" if _conv >= 0.7 else "#f85149"
            stage_badge += ("<span style='background:#0d1117;color:{};font-size:9px;"
                            "font-weight:700;padding:2px 7px;border-radius:3px;"
                            "border:1px solid {};margin-right:4px'>conv x{:.2f}</span>"
                            ).format(_cc, _cc, _conv)
        if _basis:
            stage_badge += ("<span style='background:#0d1117;color:#a371f7;font-size:9px;"
                            "font-weight:700;padding:2px 7px;border-radius:3px;"
                            "border:1px solid #2d1b69;margin-right:4px'>{}</span>"
                            ).format(_basis)
        _vo = s.get("vol_oi")
        if _vo and _vo.get("flag") in ("ELEVATED", "UNUSUAL"):
            _voc = "#f0883e" if _vo["flag"] == "UNUSUAL" else "#e3b341"
            stage_badge += ("<span style='background:#0d1117;color:{c};font-size:9px;"
                            "font-weight:700;padding:2px 7px;border-radius:3px;"
                            "border:1px solid {c};margin-right:4px' "
                            "title='Alpaca volume vs Databento OI'>VOL/OI {r} {f}</span>"
                            ).format(c=_voc, r=_vo.get("ratio"), f=_vo["flag"])

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
                        "check broker for this-week {dir} expiry</div>").format(dir=direction)

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
                  letter-spacing:.5px;margin-bottom:3px'>Targets ({basis})</div>
      <div style='font-size:11px;line-height:1.7'>
        <span style='color:#8b949e'>T1 </span>
        <span style='color:#58a6ff;font-family:monospace'>${t1}</span>
        <span style='color:#6e7681'>{t1p}</span><br>
        <span style='color:#8b949e'>T2 </span>
        <span style='color:#a371f7;font-family:monospace'>${t2}</span>
        <span style='color:#6e7681'>{t2p}</span><br>
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
            t1p        = "({}%)".format(s.get("t1_prob")) if s.get("t1_prob") is not None else "",
            t2p        = "({}%)".format(s.get("t2_prob")) if s.get("t2_prob") is not None else "",
            basis      = s.get("target_basis") or "FIB",
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
        cards_html = ("<div style='padding:28px;text-align:center;color:#8b949e;font-size:13px'>"
                      "Weekly scan running... check back in a moment.<br>"
                      "<a href='/' style='color:#58a6ff;font-size:11px;margin-top:8px;"
                      "display:block'>Refresh</a></div>")

    next_str = "{}s".format(secs) if secs > 0 else "running now"

    return """
<div style='font-size:10px;color:#8b949e;font-weight:700;text-transform:uppercase;
            letter-spacing:.8px;margin-bottom:4px;margin-top:4px'>
  PRIMARY &mdash; WEEKLY ({nsig} SETUP{pl}, SPY/QQQ + RS-RANKED STOCKS)
  <span style='color:#6e7681;font-weight:400;text-transform:none'>
    &nbsp;|&nbsp; next scan {next}</span>
</div>
<div style='font-size:10px;color:#8b949e;margin-bottom:10px'>
  <span style='color:#a371f7'>&#9632; O'Neil Pivot</span> &nbsp;
  <span style='color:#3fb950'>&#9632; Wyckoff Spring</span> &nbsp;
  <span style='color:#58a6ff'>&#9632; 52-Week Breakout</span> &nbsp;
  <span style='color:#e3b341'>&#9632; Earnings Cont.</span>
  &nbsp;&nbsp;|&nbsp;&nbsp; Stage 1/2 filtered &nbsp;|&nbsp;&nbsp;
  Stops at 0.618 fib &nbsp;|&nbsp;&nbsp; Current-week Friday settlement (rides to 0DTE)
</div>
{cards}""".format(
        nsig  = len(sigs),
        next  = next_str,
        pl    = "S" if len(sigs) != 1 else "",
        cards = cards_html,
    )


# =============================================
# END SWING SCANNER
# =============================================

def _refresh_market_state():
    """
    Refresh regime, premarket brief, and GEX bias.
    Called daily at market open + a few key times.
    """
    try:
        if HAS_REGIME:
            regime = regime_filter.classify_regime()
            # Compression-squeeze override: if today's RV20 is in the bottom
            # 20% of trailing year, gap is tight, and VIX term structure is
            # not in backwardation, flip COMPRESSED -> EXPANSION_WATCH so the
            # scanner doesn't fade a coiled-spring expansion day.
            try:
                gap_pct_abs = None
                spy_daily   = get_daily("SPY")
                spy_intra   = get_intraday("SPY")
                if spy_daily and spy_intra:
                    gp, _gd = get_premarket_gap(spy_daily, spy_intra)
                    if gp is not None:
                        gap_pct_abs = abs(gp)
                regime = regime_filter.apply_expansion_override(
                    regime, gap_pct_abs=gap_pct_abs, symbol="SPY"
                )
            except Exception as e:
                log("expansion override error: {}".format(e))
            with _market_state_lock:
                _market_state["regime"]    = regime
                _market_state["regime_ts"] = time.time()
            log("Regime refreshed: {} (expansion_watch={})".format(
                regime.get("regime"), regime.get("expansion_watch")))
    except Exception as e:
        log("regime refresh error: {}".format(e))

    try:
        if HAS_OVERNIGHT:
            spy_daily = get_daily("SPY")
            if spy_daily and len(spy_daily) >= 2:
                prev = spy_daily[-2]
                today_intraday = get_intraday("SPY")
                rth_open = today_intraday[0]["o"] if today_intraday else None
                brief = overnight_context.get_premarket_brief(
                    prev_rth_close = prev["c"],
                    prev_rth_high  = prev["h"],
                    prev_rth_low   = prev["l"],
                    rth_open       = rth_open,
                )
                with _market_state_lock:
                    _market_state["premarket_brief"] = brief
                log("Premarket brief refreshed")
    except Exception as e:
        log("premarket brief refresh error: {}".format(e))

    try:
        if HAS_GEX:
            bias = gamma_exposure.get_gex_bias("SPY")
            with _market_state_lock:
                _market_state["gex_bias"] = bias
            log("GEX bias: {} ({})".format(
                bias.get("tape_bias"), bias.get("note", "")))
    except Exception as e:
        log("GEX bias load error: {}".format(e))


def _refresh_volume_profiles():
    """Once-daily rebuild of bar-of-day volume profiles."""
    if not HAS_VOLUME_TRUTH:
        return
    try:
        all_syms = list(set(SYMBOLS + SWING_SYMBOLS))
        built = volume_truth.refresh_all(all_syms)
        log("Volume profiles refreshed for {} symbols".format(len(built)))
    except Exception as e:
        log("volume profile refresh error: {}".format(e))


def _refresh_earnings_calendar():
    """Once-daily refresh of upcoming earnings dates via yfinance."""
    if not HAS_SAFETY_GATES:
        return
    try:
        all_syms = list(set(SYMBOLS + SWING_SYMBOLS))
        ok_count = 0
        for s in all_syms:
            if safety_gates.update_earnings_calendar(s):
                ok_count += 1
        log("Earnings calendar refreshed for {}/{} symbols".format(
            ok_count, len(all_syms)))
    except Exception as e:
        log("earnings calendar refresh error: {}".format(e))


def _sweep_marker_get(key):
    """Last ET date (YYYY-MM-DD) a costly Databento sweep completed OK."""
    try:
        conn = db_utils.connect(DB_FILE)
        conn.execute("CREATE TABLE IF NOT EXISTS daily_marker "
                      "(key TEXT PRIMARY KEY, ymd TEXT)")
        row = conn.execute("SELECT ymd FROM daily_marker WHERE key = ?",
                           (key,)).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _sweep_marker_set(key, ymd):
    try:
        conn = db_utils.connect(DB_FILE)
        conn.execute("CREATE TABLE IF NOT EXISTS daily_marker "
                      "(key TEXT PRIMARY KEY, ymd TEXT)")
        conn.execute("INSERT OR REPLACE INTO daily_marker (key, ymd) "
                     "VALUES (?, ?)", (key, ymd))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _sweep_already_done_today(key):
    """True if this sweep already completed today (ET). Persists across
    process restarts, so a Railway redeploy after 16:30 ET does not
    re-run a full Databento sweep the daily job already paid for."""
    et = pytz.timezone("America/New_York")
    return _sweep_marker_get(key) == datetime.now(et).date().isoformat()


def _refresh_gex_snapshots():
    """
    End-of-day chain snapshot: builds GEX, saves OI history for delta tracking.
    Uses Databento (or Polygon fallback).
    """
    if not HAS_GEX:
        return
    if _sweep_already_done_today("gex"):
        log("GEX snapshot already completed today; skipping (cost guard)")
        return
    # Databento is the sole chain provider for GEX.
    try:
        import databento_adapter
        if not databento_adapter.is_available():
            return
    except ImportError:
        return

    # Pre-flight: if the Databento billing breaker is already open, do
    # NOT iterate every symbol and emit a noisy snapshot_empty per name.
    # Log one clear deferred line; the daily marker stays unset so the
    # next run (after cooldown / next day's quota) retries cleanly.
    if _databento_blocked():
        import databento_adapter as _da
        log_event("gex.snapshot_deferred", level="warn",
                  reason="databento_billing_blocked",
                  until=_da.billing_status().get("blocked_until"))
        return

    built = 0
    try:
        for sym in ("SPY", "QQQ"):
            spot = get_current_price(sym)
            if not spot:
                log_event("gex.snapshot_skipped", level="warn",
                          symbol=sym, reason="no_spot_price")
                continue
            gex = gamma_exposure.refresh_gex(sym, spot)
            if gex:
                built += 1
                log_event("gex.snapshot_built", symbol=sym,
                          gex_b=round(gex.get("total_gex", 0) / 1e9, 2))
            else:
                # Diagnostic: probe the chain directly so the log says
                # which leg was empty (chain itself, vs. compute step
                # downstream). databento_adapter.get_options_chain_snapshot
                # is cached so this is essentially free on the same scan.
                import databento_adapter as _da
                chain_probe = _da.get_options_chain_snapshot(sym) or []
                log_event("gex.snapshot_empty", level="warn", symbol=sym,
                          spot=spot,
                          chain_len=len(chain_probe),
                          databento_blocked=_databento_blocked())
    except Exception as e:
        log_event("gex.snapshot_error", level="error", error=str(e))

    # Only mark done on success, so a genuine failure still retries on the
    # next restart rather than being suppressed by the cost guard.
    if built > 0:
        et = pytz.timezone("America/New_York")
        _sweep_marker_set("gex", datetime.now(et).date().isoformat())


def _refresh_oi_snapshots():
    """
    Daily OI snapshot for OI-delta tracking. Pulls chain data for all
    tracked symbols and persists strike-level OI to history DB.

    Heavier query than GEX (we go per-symbol for the full universe rather
    than just SPY/QQQ), but still cheap on Databento — definitions+statistics
    schema typically <$0.01 per symbol.
    """
    if not HAS_OI_DELTA:
        return
    if _sweep_already_done_today("oi"):
        log("OI snapshot already completed today; skipping (cost guard)")
        return
    try:
        import databento_adapter
        if not databento_adapter.is_available():
            return
    except ImportError:
        return

    # Pre-flight: if the Databento billing breaker is already open, do
    # not loop ~72 symbols just to emit per-symbol failures. One clean
    # deferred line; daily marker stays unset so the next attempt retries.
    if _databento_blocked():
        import databento_adapter as _da
        log_event("oi.snapshot_deferred", level="warn",
                  reason="databento_billing_blocked",
                  until=_da.billing_status().get("blocked_until"))
        return

    all_syms = list(set(SYMBOLS + SWING_SYMBOLS))
    ok_count = 0
    error_count = 0
    for sym in all_syms:
        try:
            # ETFs pull with_price so the snapshot also carries per-contract
            # volume (the ohlcv-1d pull), giving a Databento volume to
            # cross-confirm Alpaca's on the vol/OI flag. Stocks stay on the
            # cheap OI-only sweep (Alpaca supplies their volume side).
            with_price = sym in ETF_PRODUCTS
            chain = databento_adapter.get_options_chain_snapshot(
                sym, with_price=with_price)
            if chain:
                rows = oi_delta.save_snapshot(sym, chain)
                if rows > 0:
                    ok_count += 1
                else:
                    error_count += 1
            else:
                error_count += 1
        except Exception as e:
            error_count += 1
            if error_count <= 3:  # only log first few errors
                log("OI snapshot {} error: {}".format(sym, e))

    log("OI snapshots: {}/{} symbols stored".format(ok_count, len(all_syms)))

    # Mark done only if we actually stored data, so a billing-breaker /
    # network failure still retries on the next restart.
    if ok_count > 0:
        et = pytz.timezone("America/New_York")
        _sweep_marker_set("oi", datetime.now(et).date().isoformat())


def _build_market_profile():
    """Build yesterday's RTH Market Profile for ES (and SPY as proxy)."""
    if not HAS_MPROFILE:
        return
    try:
        prof = market_profile.build_rth_profile("ES")
        if prof:
            log("Market Profile (ES): POC={} VAH={} VAL={}".format(
                prof["poc"], prof["vah"], prof["val"]))
    except Exception as e:
        log("Market Profile build error: {}".format(e))


def _pull_opening_flow():
    """
    Pull options flow in 8:00–10:00 ET window for SPY + QQQ.
    Triggered at 10:05 AM ET so the full window is past.
    """
    if not HAS_OPT_FLOW:
        return
    try:
        for sym in ("SPY", "QQQ"):
            try:
                flow = options_flow.pull_opening_flow(sym)
                if flow:
                    classification = options_flow.classify_flow(flow)
                    log("Opening flow {}: {} (imbalance {:+.2f})".format(
                        sym, classification["label"], classification["imbalance"]))
            except Exception as e:
                log("Flow pull {} error: {}".format(sym, e))
    except Exception as e:
        log("Opening flow error: {}".format(e))


def _refresh_iv_snapshots():
    """
    Daily ATM IV snapshot for every symbol. Builds the rolling 1yr history
    needed for IV Rank / IV Percentile. Skips symbols where Alpaca options
    snapshot is unavailable.
    """
    if not HAS_IV_RANK:
        return
    try:
        all_syms = list(set(SYMBOLS + SWING_SYMBOLS))
        ok = 0
        no_spot = []
        no_iv   = []
        for sym in all_syms:
            spot = get_current_price(sym)
            if not spot:
                no_spot.append(sym)
                continue
            iv = iv_rank.snapshot_symbol(sym, spot)
            if iv:
                ok += 1
            else:
                no_iv.append(sym)
        log("IV snapshots stored for {}/{} symbols".format(ok, len(all_syms)))
        if no_spot:
            log("  IV skipped (no spot): {}".format(",".join(sorted(no_spot))))
        if no_iv:
            log("  IV skipped (no chain/iv): {}".format(",".join(sorted(no_iv))))
    except Exception as e:
        log("IV snapshot error: {}".format(e))


# =============================================
# PREMARKET BRIEF (single morning alert, low-cost)
# =============================================
#
# This is the heart of "premarket mode" — one Databento batch query at
# 9:10 AM ET (20 min before the open) that pulls everything you need to
# know before the bell:
#
#   - Overnight ES/NQ range + inventory classification
#   - VIX previous close → regime classification
#   - GEX snapshot from yesterday's close
#   - High-IVR watchlist (earnings setups)
#   - Strategy-enable matrix for today
#
# Cost: roughly 1 MB of historical data per run = under $0.10/day.
# Sends one Telegram alert with all of this packaged.

def run_premarket_brief():
    """
    Run once at 9:10 AM ET. Populates _market_state and fires a single
    consolidated Telegram alert with everything you need before the open.
    """
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    today = now.strftime("%Y-%m-%d")

    log("=== Premarket brief generating ({}) ===".format(today))

    # Run market state refresh (handles regime, premarket, gex_bias)
    _refresh_market_state()

    with _market_state_lock:
        regime    = _market_state.get("regime")
        premarket = _market_state.get("premarket_brief")
        gex_bias  = _market_state.get("gex_bias")

    # Top high-IVR names for earnings IV crush watchlist. Each entry is
    # (symbol, iv_rank, iv_rv_ratio, vrp_favorable). VRP-favorable names
    # bubble to the top of the brief.
    high_ivr = []
    if HAS_IV_RANK:
        try:
            for sym in sorted(set(SYMBOLS + SWING_SYMBOLS)):
                d = iv_rank.compute_iv_rank(sym)
                if d and d["iv_rank"] >= 70:
                    high_ivr.append((
                        sym,
                        d["iv_rank"],
                        d.get("iv_rv_ratio"),
                        d.get("vrp_favorable"),
                    ))
            # Favorable VRP first, then by IV rank desc.
            high_ivr.sort(key=lambda x: (0 if x[3] else 1, -x[1]))
            high_ivr = high_ivr[:5]
        except Exception:
            pass

    # Build the alert
    msg_lines = ["☀️ PREMARKET BRIEF — " + today, ""]

    # Regime line
    if regime:
        msg_lines.append("REGIME: {}".format(regime.get("regime", "UNKNOWN")))
        if regime.get("vix"):
            msg_lines.append("  VIX: {:.2f}".format(regime["vix"]))
        if regime.get("realized"):
            msg_lines.append("  RV20: {:.1f}%".format(regime["realized"]))
        if regime.get("note"):
            msg_lines.append("  {}".format(regime["note"]))
        msg_lines.append("")

    # Overnight context
    if premarket:
        es = premarket.get("es_overnight") or {}
        if es:
            msg_lines.append("ES OVERNIGHT:")
            msg_lines.append("  Range: {} – {}".format(es.get("low"), es.get("high")))
            msg_lines.append("  Close: {} ({:.0%} of range)".format(
                es.get("last_print"), es.get("close_loc", 0)))
        inv = premarket.get("es_inventory") or {}
        if inv:
            msg_lines.append("  Inventory: {} → bias {}".format(
                inv.get("category"), inv.get("bias")))
        gap = premarket.get("gap") or {}
        if gap and gap.get("class"):
            msg_lines.append("  Gap: {} ({}%) — {}".format(
                gap.get("class"), gap.get("gap_pct"),
                gap.get("note", "").split(" → ")[-1][:60]))
        msg_lines.append("")

    # GEX bias
    if gex_bias and gex_bias.get("gex_b") is not None:
        msg_lines.append("DEALER GAMMA (SPY):")
        msg_lines.append("  GEX: ${}B ({})".format(
            gex_bias["gex_b"], gex_bias.get("regime", "?")))
        msg_lines.append("  Tape bias: {}".format(gex_bias.get("tape_bias", "?")))
        if gex_bias.get("call_wall"):
            msg_lines.append("  Call wall: {}".format(gex_bias["call_wall"]))
        if gex_bias.get("put_wall"):
            msg_lines.append("  Put wall: {}".format(gex_bias["put_wall"]))
        if gex_bias.get("flip"):
            msg_lines.append("  Zero-gamma: {}".format(gex_bias["flip"]))
        msg_lines.append("")

    # Strategy matrix for today
    if regime and regime.get("rules"):
        rules = regime["rules"]
        enabled  = [k for k in ("orb","vwap_trend","vwap_mr","ib_extension","swing_breakout")
                    if rules.get(k)]
        disabled = [k for k in ("orb","vwap_trend","vwap_mr","ib_extension","swing_breakout")
                    if not rules.get(k)]
        msg_lines.append("STRATEGIES TODAY:")
        if enabled:
            msg_lines.append("  ✓ " + ", ".join(enabled))
        if disabled:
            msg_lines.append("  ✗ " + ", ".join(disabled))
        msg_lines.append("  Conviction: x{:.2f}".format(rules.get("conviction_multiplier", 1.0)))
        msg_lines.append("")

    # Top IVR for earnings IV crush. Tag with VRP ratio so we can see at a
    # glance whether IV is actually rich vs realized -- a 70 IVR with a 1.0
    # ratio is a trap (vol earned what it priced).
    if high_ivr:
        msg_lines.append("HIGH IV RANK (>70):")
        for sym, ivr, ratio, vrp_ok in high_ivr:
            tag = ""
            if ratio is not None:
                marker = "✅" if vrp_ok else "⚠️"
                tag = "  {} VRP {:.2f}x".format(marker, ratio)
            msg_lines.append("  {} — IVR {:.0f}{}".format(sym, ivr, tag))
        msg_lines.append("")

    # Plain-English summary line
    # Pass rules so the plan can check which strategies are actually enabled today
    plan_rules = regime["rules"] if (regime and regime.get("rules")) else {}
    if regime and gex_bias:
        msg_lines.append("PLAN: " + _summarize_plan(regime, gex_bias, premarket, plan_rules))

    msg = "\n".join(msg_lines)
    send_telegram(msg)
    log("Premarket brief sent ({} chars)".format(len(msg)))


import plan_summary as _plan_summary


def _summarize_plan(regime, gex_bias, premarket, rules=None):
    """Delegates to plan_summary.summarize_plan (extracted for unit testing).

    `rules` is accepted for backward-compat with the call site in
    run_premarket_brief but ignored -- plan_summary reads what it needs
    (expansion_watch, intraday_flip, term_structure, tape_bias, regime label)
    directly from the regime dict and gex_bias dict.
    """
    return _plan_summary.summarize_plan(regime, gex_bias, premarket)


def _check_ib_extension_unlock(spy_intraday_bars):
    """
    Detect when SPY has extended beyond its 30-min initial balance with
    confirmation volume, and unlock trend strategies for the rest of the
    session. Called every scan cycle from scan_all_symbols.

    IB definition: first 6 x 5-min bars (9:30 - 10:00 ET).
    Unlock trigger:
      - Current price > IB_high + IB_range   (full extension up), OR
      - Current price < IB_low  - IB_range   (full extension down)
      - AND the extending bar's volume > 1.3x recent average

    Once unlocked, persists for the rest of the day (state cleared on date roll).
    """
    et      = pytz.timezone("America/New_York")
    today   = datetime.now(et).strftime("%Y-%m-%d")

    with _ib_unlock_lock:
        if _ib_unlock_state["date"] != today:
            # New day -- reset state
            _ib_unlock_state.update({
                "date":      today,
                "unlocked":  False,
                "direction": None,
                "ib_high":   None,
                "ib_low":    None,
                "ib_range":  None,
            })
        if _ib_unlock_state["unlocked"]:
            return _ib_unlock_state["direction"]

    if not spy_intraday_bars or len(spy_intraday_bars) < ORB_BARS + 1:
        return None

    ib_bars  = spy_intraday_bars[:ORB_BARS]
    ib_high  = max(b["h"] for b in ib_bars)
    ib_low   = min(b["l"] for b in ib_bars)
    ib_range = ib_high - ib_low
    if ib_range <= 0:
        return None

    recent_bars = spy_intraday_bars[ORB_BARS:]
    if not recent_bars:
        return None

    # Average volume over IB for the volume confirmation gate
    ib_avg_vol = statistics.mean([b["v"] for b in ib_bars]) or 1.0

    # Scan post-IB bars for an extension event
    for b in recent_bars:
        if b["h"] > ib_high + ib_range and b["v"] > ib_avg_vol * 1.3:
            with _ib_unlock_lock:
                _ib_unlock_state.update({
                    "date": today, "unlocked": True, "direction": "UP",
                    "ib_high": ib_high, "ib_low": ib_low, "ib_range": ib_range,
                })
            log("IB extension UP confirmed: bar high={:.2f} > {:.2f} (vol {:.0f} vs avg {:.0f})".format(
                b["h"], ib_high + ib_range, b["v"], ib_avg_vol))
            return "UP"
        if b["l"] < ib_low - ib_range and b["v"] > ib_avg_vol * 1.3:
            with _ib_unlock_lock:
                _ib_unlock_state.update({
                    "date": today, "unlocked": True, "direction": "DOWN",
                    "ib_high": ib_high, "ib_low": ib_low, "ib_range": ib_range,
                })
            log("IB extension DOWN confirmed: bar low={:.2f} < {:.2f} (vol {:.0f} vs avg {:.0f})".format(
                b["l"], ib_low - ib_range, b["v"], ib_avg_vol))
            return "DOWN"

    return None


def _intraday_regime_recheck():
    """
    Run once around 10:30 AM ET. Compares 30-min realized vol from 5-min SPY
    bars against the morning's regime classification. If the intraday tape is
    materially more volatile than the COMPRESSED label implies, force-flip to
    LOW_VOL and fire a Telegram update so the user knows the matrix changed.
    """
    if not HAS_REGIME:
        return

    et       = pytz.timezone("America/New_York")
    today    = datetime.now(et).strftime("%Y-%m-%d")
    if _regime_recheck_done.get("date") == today:
        return

    intraday = get_intraday("SPY")
    # Need at least 12 5-min bars (60 min) of data for a meaningful read
    if not intraday or len(intraday) < 12:
        return

    closes = [b["c"] for b in intraday[-12:]]
    rets   = []
    for i in range(1, len(closes)):
        if closes[i-1] <= 0:
            return
        rets.append(math.log(closes[i] / closes[i-1]))
    if len(rets) < 2:
        return

    # Annualize 5-min realized vol: 78 5-min bars/day * 252 trading days = 19656
    sd        = statistics.stdev(rets)
    rv_intra  = sd * math.sqrt(19656) * 100   # percent annualized

    with _market_state_lock:
        regime_now = _market_state.get("regime") or {}
    current_label = regime_now.get("regime")
    rv20          = regime_now.get("realized") or 0

    # Flip rule: intraday RV is 1.3x trailing 20-day, AND we're currently
    # in COMPRESSED. Any other transition we ignore for now.
    should_flip = (
        current_label == "COMPRESSED"
        and rv20 > 0
        and rv_intra > rv20 * 1.3
    )

    _regime_recheck_done["date"]    = today
    _regime_recheck_done["flipped"] = should_flip
    _regime_recheck_done["from"]    = current_label
    _regime_recheck_done["rv_intra"] = round(rv_intra, 2)

    if not should_flip:
        log("Intraday regime re-check: {} (rv_intra={:.1f}%, rv20={:.1f}%) - no flip".format(
            current_label, rv_intra, rv20))
        return

    new_label = "LOW_VOL"
    _regime_recheck_done["to"] = new_label

    new_regime = dict(regime_now)
    new_regime["regime"]   = new_label
    new_regime["rules"]    = dict(regime_filter.REGIME_STRATEGY_RULES[new_label])
    new_regime["note"]     = "INTRADAY_FLIP from COMPRESSED. RV_intra={:.1f}% (RV20={:.1f}%)".format(
        rv_intra, rv20)
    new_regime["intraday_flip"] = True
    new_regime["rv_intra"]      = round(rv_intra, 2)

    with _market_state_lock:
        _market_state["regime"] = new_regime

    log("REGIME FLIP: COMPRESSED -> LOW_VOL (rv_intra={:.1f}%)".format(rv_intra))
    try:
        send_telegram(
            "REGIME UPDATE: COMPRESSED -> LOW_VOL\n"
            "Intraday RV expanding ({:.1f}% vs RV20 {:.1f}%).\n"
            "Trend strategies fully active for the rest of the session."
            .format(rv_intra, rv20)
        )
    except Exception as e:
        log("regime flip telegram error: {}".format(e))


def _check_overnight_gamma_reversal():
    """Run at 3:50 PM ET. If short-gamma + intraday weakness, fire a signal."""
    if not HAS_NEW_STRATS:
        return
    try:
        et      = pytz.timezone("America/New_York")
        now_et  = datetime.now(et)
        spy_intraday = get_intraday("SPY")
        if not spy_intraday or len(spy_intraday) < 2:
            return
        spy_close_now = spy_intraday[-1]["c"]
        spy_open      = spy_intraday[0]["o"]
        if not spy_open:
            return
        pct_today = (spy_close_now - spy_open) / spy_open * 100

        sig = new_strategies.detect_overnight_gamma_reversal(
            spy_close_price       = spy_close_now,
            current_time_et       = now_et,
            intraday_close_pct    = pct_today,
        )
        if sig and should_alert("SPY", "OVERNIGHT_GAMMA_REVERSAL"):
            msg = (
                "🌙 OVERNIGHT GAMMA REVERSAL\n\n"
                "Buy SPY @ ${}\n"
                "Stop ${} | Target ${}\n"
                "Exit 9:35 ET tomorrow\n\n"
                "GEX: ${}B | Today: {:.2f}%"
            ).format(sig["entry"], sig["stop"], sig["target"],
                     sig["gex_b"], sig["intraday_pct"])
            send_telegram(msg)
            db_log_signal({
                "symbol": "SPY", "direction": "CALL",
                "premium": None, "contracts": 1,
                "stop": sig["stop"], "target": sig["target"],
                "grade": "OG", "grade_pts": sig["score"],
                "signal_type": "OVERNIGHT_GAMMA_REVERSAL",
            })
            log("Overnight gamma reversal signal fired")
    except Exception as e:
        log("overnight gamma check error: {}".format(e))


_daily_refresh_done = {"date": None, "vol": False, "earnings": False, "gex": False}
_premarket_done     = {"date": None}
_overnight_done     = {"date": None}

# IB-extension unlock: once SPY breaks its 30-min initial balance with volume,
# all trend strategies are re-enabled at full grading for the rest of the day
# regardless of the morning regime label. Resets daily.
_ib_unlock_state = {
    "date":      None,
    "unlocked":  False,
    "direction": None,   # "UP" or "DOWN"
    "ib_high":   None,
    "ib_low":    None,
    "ib_range":  None,
}
_ib_unlock_lock = threading.Lock()

# Intraday regime re-classify: runs once at ~10:30 ET. Sets a marker so we
# don't double-fire and so the brief endpoint can show whether a flip happened.
_regime_recheck_done = {"date": None, "flipped": False, "from": None, "to": None}


def background_scheduler():
    global _ai_last_run_date
    log("Background scheduler started")
    time.sleep(10)

    # --- Boot-time bootstrap ---
    # If we boot mid-day, all the morning refreshes were missed. Run them
    # now so the dashboard isn't blank.
    et       = pytz.timezone("America/New_York")
    boot_now = datetime.now(et)
    boot_hour = boot_now.hour + boot_now.minute / 60.0
    today_str = boot_now.strftime("%Y-%m-%d")

    log("Boot-time bootstrap @ {:.2f} ET (mode: {})".format(boot_hour, OPERATING_MODE))
    threading.Thread(target=_refresh_market_state, daemon=True).start()
    if boot_hour >= 6.0:
        threading.Thread(target=_refresh_earnings_calendar, daemon=True).start()
    if boot_hour >= 8.0:
        threading.Thread(target=_refresh_volume_profiles, daemon=True).start()
        # Build yesterday's Market Profile (needed for today's opening classification)
        threading.Thread(target=_build_market_profile, daemon=True).start()
    # Daily jobs only run on a trading session (skip weekends/holidays).
    _is_session = is_trading_day()
    # Brief on boot ONLY if we're in the pre-open window (9:00 AM – 10:30 AM ET)
    if _is_session and 9.0 <= boot_hour <= 10.5:
        threading.Thread(target=run_premarket_brief, daemon=True).start()
    # Opening flow: pull at boot if we're past 10:05 AM today
    if _is_session and 10.1 <= boot_hour <= 16.0:
        threading.Thread(target=_pull_opening_flow, daemon=True).start()
    if _is_session and boot_hour >= 16.25:
        threading.Thread(target=_refresh_iv_snapshots, daemon=True).start()
    if _is_session and boot_hour >= 16.5:
        threading.Thread(target=_refresh_gex_snapshots, daemon=True).start()
        threading.Thread(target=_refresh_oi_snapshots, daemon=True).start()

    # Mark today's refreshes as done so we don't double-run
    _daily_refresh_done["date"]       = today_str
    _daily_refresh_done["earnings"]   = boot_hour >= 6.0
    _daily_refresh_done["vol"]        = boot_hour >= 8.0
    _daily_refresh_done["mprofile"]   = boot_hour >= 8.0
    _daily_refresh_done["flow"]       = boot_hour >= 10.1
    _daily_refresh_done["premarket"]  = (boot_hour >= 8.5)
    _daily_refresh_done["gex"]        = boot_hour >= 16.5
    _daily_refresh_done["oi"]         = boot_hour >= 16.5
    _daily_refresh_done["iv"]         = boot_hour >= 16.25
    _daily_refresh_done["paper"]      = boot_hour >= 16.05
    # Only relevant on Fridays; pre-mark on non-Fridays so it never fires.
    _is_friday = datetime.now(et).weekday() == 4
    _daily_refresh_done["friday_digest"] = (not _is_friday) or (boot_hour >= 16.25)
    _premarket_done["date"]           = today_str if boot_hour >= 9.4 else None
    _overnight_done["date"]           = today_str if boot_hour >= 15.93 else None

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

            # --- Daily maintenance tasks ---
            # Pre-market (6 AM ET): refresh earnings + volume profiles
            if et_hour >= 6.0 and _daily_refresh_done.get("date") != today:
                _daily_refresh_done["date"] = today
                _daily_refresh_done["earnings"]  = False
                _daily_refresh_done["vol"]       = False
                _daily_refresh_done["gex"]       = False
                _daily_refresh_done["iv"]        = False
                _daily_refresh_done["premarket"] = False
                _daily_refresh_done["mprofile"]  = False
                _daily_refresh_done["flow"]      = False
                _daily_refresh_done["oi"]        = False
                _daily_refresh_done["paper"]     = False
                # Pre-mark non-Fridays so the digest never fires on Mon-Thu.
                _daily_refresh_done["friday_digest"] = (
                    datetime.now(et).weekday() != 4
                )
            # Holiday/weekend-aware: gates the premarket brief and every
            # EOD job so they don't fire (or spend on Databento) when the
            # market is closed. Cached per-day, so this is one cheap call.
            _session = is_trading_day()

            if et_hour >= 6.0 and not _daily_refresh_done.get("earnings"):
                threading.Thread(target=_refresh_earnings_calendar, daemon=True).start()
                _daily_refresh_done["earnings"] = True
            if et_hour >= 8.0 and not _daily_refresh_done.get("vol"):
                threading.Thread(target=_refresh_volume_profiles, daemon=True).start()
                _daily_refresh_done["vol"] = True
            # 8:00 AM ET: build yesterday's RTH Market Profile
            # (yesterday's session has fully closed by then)
            if et_hour >= 8.0 and not _daily_refresh_done.get("mprofile"):
                threading.Thread(target=_build_market_profile, daemon=True).start()
                _daily_refresh_done["mprofile"] = True

            # --- PREMARKET BRIEF (9:10 AM ET, once daily) ---
            # Fires 20 min before the bell so it captures the full overnight
            # + premarket picture (futures gap, options flow, news pricing-in)
            # without missing the action that builds in the final hour of
            # premarket. In premarket mode, this is THE main alert of the day.
            if (_session and et_hour >= 9.166 and et_hour < 9.34
                    and not _daily_refresh_done.get("premarket")):
                _daily_refresh_done["premarket"] = True
                threading.Thread(target=run_premarket_brief, daemon=True).start()

            # 10:05 AM ET: pull SPY+QQQ options flow for the 8:00-10:00 window
            if (_session and et_hour >= 10.08 and et_hour < 10.3
                    and not _daily_refresh_done.get("flow")):
                _daily_refresh_done["flow"] = True
                threading.Thread(target=_pull_opening_flow, daemon=True).start()

            # 10:30 AM ET: re-classify regime against intraday RV.
            # If we labeled today COMPRESSED but the tape is expanding, force
            # a flip and notify so the matrix reflects reality.
            if (et_hour >= 10.5 and et_hour < 10.8
                    and _regime_recheck_done.get("date") != today):
                threading.Thread(target=_intraday_regime_recheck, daemon=True).start()

            # --- The following are INTRADAY refreshes ---
            # Skip them in 'premarket' mode to save Databento spend.
            if OPERATING_MODE == "continuous":
                # 9:25 AM ET: refresh regime/premarket/GEX
                if (et_hour >= 9.4 and et_hour < 9.6
                        and _premarket_done.get("date") != today):
                    _premarket_done["date"] = today
                    threading.Thread(target=_refresh_market_state, daemon=True).start()

                # 3:45–3:55 PM ET: overnight gamma reversal check
                if (et_hour >= 15.75 and et_hour < 15.93
                        and _overnight_done.get("date") != today):
                    _overnight_done["date"] = today
                    threading.Thread(target=_check_overnight_gamma_reversal, daemon=True).start()

            # 4:30 PM ET: post-close GEX snapshot (both modes — feeds tomorrow's brief)
            if _session and et_hour >= 16.5 and not _daily_refresh_done.get("gex"):
                threading.Thread(target=_refresh_gex_snapshots, daemon=True).start()
                _daily_refresh_done["gex"] = True

            # 4:35 PM ET: OI snapshot for delta tracking (full universe)
            if _session and et_hour >= 16.58 and not _daily_refresh_done.get("oi"):
                threading.Thread(target=_refresh_oi_snapshots, daemon=True).start()
                _daily_refresh_done["oi"] = True

            # 4:15 PM ET: daily IV snapshot (both modes — builds rolling history)
            if _session and et_hour >= 16.25 and not _daily_refresh_done.get("iv"):
                threading.Thread(target=_refresh_iv_snapshots, daemon=True).start()
                _daily_refresh_done["iv"] = True

            # 0. Paper trader: replay today's signals at 4:04 PM ET so the
            #    AI improvement step below sees synthetic outcomes alongside
            #    any manually-logged real trades. Runs once per day.
            if (_session and HAS_PAPER_TRADER and et_hour >= 16.05
                    and not _daily_refresh_done.get("paper")):
                _daily_refresh_done["paper"] = True
                def _paper_run():
                    try:
                        paper_trader.run_paper_trader(DB_FILE, log_fn=log)
                    except Exception as _e:
                        log("paper trader error: {}".format(_e))
                threading.Thread(target=_paper_run, daemon=True).start()

            # 1. Weekly AI improvement: once per week, Friday after the
            #    close. weekday()==4 is Friday; _ai_last_run_date != today
            #    keeps it to a single run that day. The Friday digest below
            #    fires ~17 min later so it reports the fresh analysis.
            if (_session and et_hour >= 16.08
                    and datetime.now(et).weekday() == 4
                    and _ai_last_run_date != today):
                log("AI: Weekly Friday trigger")
                threading.Thread(
                    target=run_ai_improvement,
                    args=("weekly_friday",),
                    daemon=True
                ).start()

            # 1b. Friday weekly upgrade digest: ~10 min after EOD AI so the
            #     latest analysis is committed. Opus-generated, Telegram-sent.
            if (_session and et_hour >= 16.25
                    and datetime.now(et).weekday() == 4
                    and not _daily_refresh_done.get("friday_digest")):
                _daily_refresh_done["friday_digest"] = True
                log("AI: Friday digest trigger")
                threading.Thread(target=run_friday_digest, daemon=True).start()

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
            take_url = ("/take?sym={}&dir={}&prem={}&stp={}&tgt={}"
                        "&grade={}&gpts={}&gap={:.2f}&gdir={}&rs={:.2f}"
                        "&horizon=INTRADAY").format(
                sym, d, prem, stp_opt, tgt_opt,
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
            Stop ${sopt} &nbsp;|&nbsp; Target ${topt}
          </div>
        </div>
        <a href='{url}' style='background:#238636;color:#fff;padding:10px 20px;
           border-radius:6px;text-decoration:none;font-size:13px;font-weight:700;
           letter-spacing:.3px;white-space:nowrap'>LOG TRADE</a>
      </div>""".format(
                prem=prem, sopt=stp_opt, topt=tgt_opt,
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
        # BUG FIX (was: (cp - t["premium"]) * 100 * contracts which mixed
        # underlying price with option premium, producing absurd values).
        #
        # We don't poll option prices continuously, so we estimate the option
        # P&L from the underlying's move using a rough delta. ATM options have
        # delta ~0.50 at entry; we assume that for unrealized estimates.
        # If we have a stored entry underlying we use the actual move; else
        # we hide P&L until close.
        unreal = None
        if cp and t.get("premium") and t.get("entry_under"):
            entry_under = t["entry_under"]
            move        = cp - entry_under
            if t["direction"] == "PUT":
                move = -move
            # Assumed delta ~0.50 for ATM at entry; gamma scaling ignored.
            est_opt_change = move * 0.50
            # Per-contract basis (position size no longer modeled).
            unreal = round(est_opt_change * 100, 2)
        elif cp and t.get("premium"):
            # No entry_under stored (older trade) — display dash
            pass

        if unreal is None:
            us = "<span style='color:#8b949e' title='Awaiting option mid quote'>~</span>"
        else:
            uc = "#3fb950" if unreal >= 0 else "#f85149"
            us = "<span style='color:{};font-weight:600' title='Delta-estimate from underlying'>~${}</span>".format(uc, unreal)
        dc = "#3fb950" if t["direction"]=="CALL" else "#f85149"
        open_rows += (
            "<tr style='border-bottom:1px solid #21262d'>"
            "<td style='padding:9px 10px;font-weight:700'>{sym}</td>"
            "<td style='padding:9px 6px;color:{dc}'>{dir}</td>"
            "<td style='padding:9px 6px;font-family:monospace'>${prem}</td>"
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
                 prem=t["premium"],
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

    # Count all closed trades for AI readiness indicator
    all_closed_trades = db_get_all_closed_trades()
    n_closed_all      = len(all_closed_trades)
    n_open_all        = len(db_get_open_trades())
    ai_ready          = n_closed_all >= AI_MIN_TOTAL_SAMPLES
    ai_data_line = (
        "<div style='font-size:11px;margin-top:6px;padding-top:6px;"
        "border-top:1px solid #21262d'>"
        "<span style='color:{dc}'>{icon}</span> "
        "<span style='color:#8b949e'>AI Data:</span> "
        "<span style='color:#e6edf3'>{nc} closed</span>"
        "<span style='color:#8b949e'> / {no} open</span>"
        " &mdash; {status}"
        "</div>"
    ).format(
        dc="#3fb950" if ai_ready else "#e3b341",
        icon="&#10003;" if ai_ready else "&#9888;",
        nc=n_closed_all, no=n_open_all,
        status=("<span style='color:#3fb950'>Learning active</span>" if ai_ready
                else "<span style='color:#e3b341'>Need {} more closed trades"
                     " (close open trades with WIN/LOSS buttons)</span>".format(
                         AI_MIN_TOTAL_SAMPLES - n_closed_all))
    )

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
  {ai_data}
</div>""".format(ver=ai_ver, upd=ai_updated, insight=ai_insight, focus=ai_focus,
                  ai_data=ai_data_line)
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
  {ai_data}
</div>""".format(insight=ai_insight, ai_data=ai_data_line)

    html = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>SPX Engine</title>
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
    <span class="brand">SPX ENGINE</span>
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
    <a class="nav-link" href="/chat">Chat</a>
    <a class="nav-link" href="/ai">AI</a>
    <a class="nav-link" href="/stats">Stats</a>
    <a class="nav-link" href="/alpaca-test">Alpaca</a>
    <a class="nav-link" href="/debug">Debug</a>
  </div>
</div>

<!-- AI INSIGHT PANEL -->
{ai_panel}

<!-- INDEX CONTEXT -->
<div style="padding:12px 14px 0">{index_strip}</div>

<!-- WEEKLY / PRIMARY TIER (merged from the former Swing page) -->
<div style="padding:12px 14px 0">{weekly_section}</div>

<!-- SIGNAL CARDS -->
<div style="padding:12px 14px 0">
  <div style="font-size:10px;font-weight:700;color:#8b949e;
              text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px">
    SECONDARY &mdash; INTRADAY (SPY/QQQ 0DTE + liquid stock weeklies) &mdash; {nsig} setup{pl_s} found
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
        index_strip=render_index_context_strip(),
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
            "<th>Unreal P&amp;L</th><th>Close</th></tr>"
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
        log_lines="<br>".join(logs) if logs else "No log entries yet",
        weekly_section=_build_weekly_section(),
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
    stp   = request.args.get("stp", "0")
    tgt   = request.args.get("tgt", "0")
    grade = request.args.get("grade", None)
    gpts  = request.args.get("gpts", None)
    gap   = request.args.get("gap", None)
    gdir  = request.args.get("gdir", None)
    rs    = request.args.get("rs", None)
    stype = request.args.get("stype", None)
    horizon = request.args.get("horizon", "INTRADAY")
    try:
        # Capture underlying spot at trade entry — used for delta-based
        # unrealized P&L estimate on the dashboard.
        entry_under = None
        try:
            entry_under = get_current_price(sym)
        except Exception:
            pass

        # Manual trades occupy their tier too (so they mute new alerts), but are
        # only closed by the user (mode='manual', not auto-resolved).
        db_log_trade(
            sym, dir_, float(prem), None, float(stp), float(tgt),
            grade=grade,
            grade_pts=int(float(gpts)) if gpts else None,
            gap_pct=float(gap) if gap else None,
            gap_dir=gdir,
            rs=float(rs) if rs else None,
            entry_under=entry_under,
            signal_type=stype,
            horizon=horizon, mode="manual",
        )
        log("Trade taken: {} {} {} grade={} prem={} under={}".format(
            sym, dir_, grade, gpts, prem, entry_under))
        send_telegram(
            "TRADE TAKEN\n{} {} | Grade: {} ({}pts)\n"
            "Entry: ${} | Stop: ${} | Target: ${}\n"
            "Gap: {}% {} | RS vs SPY: {}%".format(
                sym, dir_, grade or "?", gpts or "?",
                prem, stp, tgt,
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
        conn = db_utils.connect(DB_FILE)
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


@app.route("/ai/reset")
def ai_reset_baseline():
    """Manual: wipe AI tuning, restore baseline, start a fresh epoch.

    Guarded by ?confirm=1 so a stray click can't nuke tuning.
    """
    if request.args.get("confirm") != "1":
        return ("<html><body style='background:#0d1117;color:#e6edf3;"
                "font-family:Arial;padding:20px'>"
                "<h2>Reset AI config to baseline?</h2>"
                "<p>This restores DEFAULT_CONFIG and makes the AI ignore "
                "all trades before now.</p>"
                "<a href='/ai/reset?confirm=1' style='background:#f85149;"
                "color:#fff;padding:8px 14px;border-radius:6px;"
                "text-decoration:none'>Yes, reset</a>&nbsp;&nbsp;"
                "<a href='/ai' style='color:#58a6ff'>Cancel</a>"
                "</body></html>")
    reset_ai_config_to_baseline(reason="manual")
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
    # The swing/weekly tier is now merged into the single unified dashboard at
    # "/". Keep the route as a redirect so old bookmarks / links still land.
    return redirect("/")


@app.route("/swing/scan")
def swing_scan_now():
    """Manually trigger the weekly pass of the unified scan."""
    threading.Thread(target=run_unified_scan, kwargs={"do_weekly": True},
                     daemon=True).start()
    return redirect("/")


@app.route("/scan/unified")
def scan_unified():
    """JSON view of the merged tiered payload (weekly + intraday + context)."""
    return jsonify(run_unified_scan())


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
                p=s.get("price",0), vwap=round(float(s.get("vwap") or 0),2),
                t1=s.get("und_call_t1") or s.get("und_put_t1") or 0,
                t2=s.get("und_call_t2") or s.get("und_put_t2") or 0,
                stp=s.get("und_call_stop") or s.get("und_put_stop") or 0,
                gap=float(s.get("gap_pct") or 0),
                rs=float(s.get("rs") or 0),
                hr=s.get("trend_1hr","?"),
                vol=s.get("time_vol_lbl","?"),
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
            "T1:{t1} T2:{t2} T3:{t3} | Stop:{stp} | "
            "RS:{rs:+.1f}%".format(
                sym=s.get("symbol","?"), d=s.get("direction","?"),
                stype=s.get("signal_type","?"), prob=s.get("prob",0),
                p=s.get("price",0),
                t1=s.get("t1") or "-", t2=s.get("t2") or "-", t3=s.get("t3") or "-",
                stp=s.get("stop","-"),
                rs=float(s.get("spy_rs") or 0),
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
            "  #{id} {sym} {d} | Entry:${e} | Stop:${stp} Target:${tgt}".format(
                id=t["id"], sym=t["symbol"], d=t["direction"],
                e=t["premium"],
                stp=t["stop"], tgt=t["target"]))

    # Market
    bias = "---"
    spy_chg = 0.0
    for s in dte_sigs:
        if s.get("market_bias"):
            bias    = s["market_bias"]
            spy_chg = s.get("spy_chg", 0.0)
            break

    # Index context (display-only SPX/NDX proxy levels)
    idx_lines = []
    try:
        for c in index_context():
            idx_lines.append("  {sym} {lvl} ({pct}) RS {rs} [via {px}]".format(
                sym=c["symbol"],
                lvl="{:,.2f}".format(c["level"]) if c.get("level") else "-",
                pct="{:+.2f}%".format(c["pct"]) if c.get("pct") is not None else "-",
                rs=c.get("rs"), px=c.get("proxy")))
    except Exception:
        pass

    ctx = """=== SCANNER CONTEXT ({time} ET) ===

MARKET: {bias} | SPY {spy:+.2f}% | {mkt}

INDEX CONTEXT (proxy levels, context only):
{idx}

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
        idx     = "\n".join(idx_lines) or "  (n/a)",
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
    <a class='nav-link' href='/'>Engine</a>
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


@app.route("/db-status")
def db_status():
    """Debug endpoint: shows DB file path, trade counts, and AI state."""
    try:
        conn = db_utils.connect(DB_FILE)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM trades WHERE outcome='OPEN'")
        open_count = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM trades WHERE outcome='WIN'")
        win_count = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM trades WHERE outcome='LOSS'")
        loss_count = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM trades")
        total_count = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM ai_analyses")
        ai_runs = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM ai_config")
        ai_configs = c.fetchone()[0]
        # Last 5 trades
        c.execute("SELECT id, symbol, direction, outcome, pnl, ts FROM trades ORDER BY id DESC LIMIT 5")
        recent = [{"id": r[0], "symbol": r[1], "dir": r[2], "outcome": r[3],
                   "pnl": r[4], "ts": r[5]} for r in c.fetchall()]
        conn.close()

        cfg = get_config()
        return jsonify({
            "db_file": DB_FILE,
            "data_dir_env": os.getenv("DATA_DIR", "(not set, using /tmp)"),
            "trades": {
                "total": total_count,
                "open": open_count,
                "wins": win_count,
                "losses": loss_count,
                "closed_total": win_count + loss_count,
                "ai_needs": "{} closed trades minimum".format(AI_MIN_TOTAL_SAMPLES),
                "ai_ready": (win_count + loss_count) >= AI_MIN_TOTAL_SAMPLES,
            },
            "ai_engine": {
                "config_version": cfg.get("ai_version", 0),
                "total_ai_runs": ai_runs,
                "total_config_saves": ai_configs,
                "last_updated_by": cfg.get("updated_by", "default"),
                "last_updated_at": cfg.get("updated_at", "never"),
                "insight": cfg.get("ai_insight", ""),
                "focus": cfg.get("ai_focus", ""),
            },
            "recent_trades": recent,
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/debug")
def debug_route():
    with state_lock:
        return jsonify({"signals": all_signals, "log": debug_log[-50:]})


# =============================================
# QUANT EDGE DASHBOARD ENDPOINTS
# =============================================

@app.route("/regime")
def regime_endpoint():
    """Current vol regime + strategy rules."""
    with _market_state_lock:
        regime = _market_state.get("regime")
    if not regime:
        return jsonify({
            "status": "no_data",
            "note":   "Regime not yet refreshed. Will populate at 9:25 AM ET.",
            "module_loaded": HAS_REGIME,
        })
    return jsonify({
        "status": "ok",
        "regime": regime,
    })


@app.route("/regime/refresh")
def regime_refresh_endpoint():
    """Force immediate regime refresh."""
    threading.Thread(target=_refresh_market_state, daemon=True).start()
    return jsonify({"status": "refresh queued"})


@app.route("/gex")
def gex_endpoint():
    """Current GEX bias + call wall + put wall + zero-gamma flip."""
    with _market_state_lock:
        bias = _market_state.get("gex_bias")
    if not bias:
        return jsonify({
            "status":        "no_data",
            "note":          "GEX not yet built. Requires DATABENTO_API_KEY and post-close refresh.",
            "module_loaded": HAS_GEX,
            "databento_key": bool(os.getenv("DATABENTO_API_KEY", "").strip()),
        })
    # Also pull the latest raw snapshot for SPY for the dashboard
    raw_spy = None
    raw_qqq = None
    if HAS_GEX:
        try:
            raw_spy = gamma_exposure.load_latest_gex("SPY")
            raw_qqq = gamma_exposure.load_latest_gex("QQQ")
        except Exception:
            pass
    return jsonify({
        "status":     "ok",
        "bias":       bias,
        "spy_latest": raw_spy,
        "qqq_latest": raw_qqq,
    })


@app.route("/overnight")
def overnight_endpoint():
    """Premarket brief: overnight futures range, inventory, gap class."""
    with _market_state_lock:
        brief = _market_state.get("premarket_brief")
    if not brief:
        return jsonify({
            "status":        "no_data",
            "note":          "Premarket brief refreshes at 9:10 AM ET.",
            "module_loaded": HAS_OVERNIGHT,
        })
    return jsonify({
        "status": "ok",
        "brief":  brief,
    })


@app.route("/iv")
def iv_endpoint():
    """IV Rank for all tracked symbols."""
    if not HAS_IV_RANK:
        return jsonify({"status": "no_module", "note": "iv_rank module not loaded"})
    out = []
    for sym in sorted(set(SYMBOLS + SWING_SYMBOLS)):
        try:
            data = iv_rank.compute_iv_rank(sym)
            if data:
                out.append({
                    "symbol":        sym,
                    "iv_today":      data["iv_today"],
                    "iv_rank":       data["iv_rank"],
                    "iv_percentile": data["iv_percentile"],
                    "rv":            data.get("rv"),
                    "iv_rv_gap_pct": data.get("iv_rv_gap_pct"),
                    "iv_rv_ratio":   data.get("iv_rv_ratio"),
                    "vrp_favorable": data.get("vrp_favorable"),
                    "samples":       data["samples"],
                })
        except Exception:
            continue
    # Sort by IV Rank descending — most "premium-rich" names first
    out.sort(key=lambda x: -x["iv_rank"])
    return jsonify({
        "status":   "ok",
        "symbols":  out,
        "count":    len(out),
    })


@app.route("/iv/coverage")
def iv_coverage_endpoint():
    """Per-symbol IV history coverage. Use to verify iv_backfill ran."""
    if not HAS_IV_RANK:
        return jsonify({"status": "no_module"})
    try:
        conn = db_utils.connect(iv_rank.IV_CACHE_DB)
        rows = conn.execute("""
            SELECT symbol, COUNT(*) as n,
                   MIN(obs_date) as first_date,
                   MAX(obs_date) as last_date
            FROM iv_history
            GROUP BY symbol
            ORDER BY symbol
        """).fetchall()
        conn.close()
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)})
    return jsonify({
        "status":  "ok",
        "symbols": [
            {"symbol": r[0], "samples": r[1],
             "first_date": r[2], "last_date": r[3],
             "ready_for_ivr": r[1] >= 30}
            for r in rows
        ],
    })


@app.route("/edge")
def edge_endpoint():
    """One-stop dashboard: combined regime + GEX + overnight + top-IVR list."""
    with _market_state_lock:
        regime    = _market_state.get("regime")
        brief     = _market_state.get("premarket_brief")
        gex_bias  = _market_state.get("gex_bias")

    # Top 5 high-IVR names
    top_iv = []
    if HAS_IV_RANK:
        try:
            for sym in sorted(set(SYMBOLS + SWING_SYMBOLS)):
                d = iv_rank.compute_iv_rank(sym)
                if d and d["iv_rank"] >= 60:
                    top_iv.append({
                        "symbol": sym,
                        "iv_rank": d["iv_rank"],
                        "iv_percentile": d["iv_percentile"],
                        "iv_rv_ratio":   d.get("iv_rv_ratio"),
                        "vrp_favorable": d.get("vrp_favorable"),
                    })
            top_iv.sort(key=lambda x: -x["iv_rank"])
            top_iv = top_iv[:5]
        except Exception:
            pass

    # Check Databento availability
    databento_available = False
    try:
        import databento_adapter
        databento_available = databento_adapter.is_available()
    except ImportError:
        pass

    return jsonify({
        "regime":             regime,
        "gex_bias":           gex_bias,
        "premarket_brief":    brief,
        "high_ivr_symbols":   top_iv,
        "modules": {
            "volume_truth":     HAS_VOLUME_TRUTH,
            "safety_gates":     HAS_SAFETY_GATES,
            "regime_filter":    HAS_REGIME,
            "overnight":        HAS_OVERNIGHT,
            "gamma_exposure":   HAS_GEX,
            "new_strategies":   HAS_NEW_STRATS,
            "iv_rank":          HAS_IV_RANK,
        },
        "api_keys": {
            "alpaca":    bool(os.getenv("APCA_API_KEY_ID", "").strip()),
            "databento": bool(os.getenv("DATABENTO_API_KEY", "").strip()),
            "anthropic": bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
            "telegram":  bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip()),
        },
        "providers_active": {
            "bars_source":      "alpaca",
            "options_iv_source":"alpaca",
            "vix_source":       "yahoo_finance",
            "earnings_source":  "yahoo_finance",
            "futures_source":   "databento" if databento_available else "alpaca_etf",
            "gex_source":       "databento" if databento_available else "disabled",
        },
    })


@app.route("/databento")
def databento_endpoint():
    """Diagnostic for Databento connectivity."""
    try:
        import databento_adapter
        return jsonify(databento_adapter.diagnostic())
    except ImportError:
        return jsonify({"error": "databento_adapter module not present"})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/brief")
def brief_endpoint():
    """
    Manually trigger the premarket brief. Useful for testing and for
    on-demand re-runs if the 9:10 AM scheduled run was missed.
    """
    threading.Thread(target=run_premarket_brief, daemon=True).start()
    return jsonify({
        "status":         "queued",
        "operating_mode": OPERATING_MODE,
        "note":           "Premarket brief queued. Check Telegram in ~30s.",
    })


@app.route("/grade-debug")
def grade_debug_endpoint():
    """
    Compute a grade for a symbol+direction and return the full breakdown,
    including how much each new edge module (OI delta, Market Profile,
    options flow) is contributing.

    Usage: /grade-debug?sym=SPY&dir=CALL
    """
    sym = request.args.get("sym", "SPY").upper()
    dir_ = request.args.get("dir", "CALL").upper()
    if dir_ not in ("CALL", "PUT"):
        return jsonify({"error": "dir must be CALL or PUT"})

    try:
        price = get_current_price(sym)
        if not price:
            return jsonify({"error": "no current price for {}".format(sym)})

        et = pytz.timezone("America/New_York")
        et_hour = datetime.now(et).hour + datetime.now(et).minute / 60.0

        # Use neutral defaults for the technical inputs so we isolate edge bonuses
        grade, pts, color, breakdown = confluence_grade(
            breakout_strength = 0.005,
            vol_ratio         = 1.5,
            vol_mult          = 1.0,
            gap_pct           = 0.3,
            gap_direction     = "UP" if dir_ == "CALL" else "DOWN",
            rs                = 0.2 if dir_ == "CALL" else -0.2,
            direction         = dir_,
            et_hour           = et_hour,
            symbol            = sym,
            spot_price        = price,
        )

        return jsonify({
            "symbol":    sym,
            "direction": dir_,
            "price":     price,
            "grade":     grade,
            "pts":       pts,
            "breakdown": breakdown,
            "note":      "Technical inputs are placeholder defaults; "
                         "use this endpoint to inspect edge bonus contributions only.",
        })
    except Exception as e:
        return jsonify({"error": str(e), "type": type(e).__name__})


@app.route("/databento-test")
def databento_raw_test():
    """
    Runs raw Databento queries and surfaces the ACTUAL error messages from
    the SDK, plus the list of accessible datasets and cost estimates.
    """
    out = {"queries": {}}
    try:
        import databento as db
        if not os.getenv("DATABENTO_API_KEY", "").strip():
            return jsonify({"error": "DATABENTO_API_KEY not set"})

        client = db.Historical(os.getenv("DATABENTO_API_KEY"))
        et = pytz.timezone("America/New_York")
        today = datetime.now(et).date()

        # List accessible datasets
        try:
            datasets = client.metadata.list_datasets()
            out["accessible_datasets"] = list(datasets) if datasets else []
        except Exception as e:
            out["accessible_datasets_error"] = "{}: {}".format(type(e).__name__, e)

        # Try a tiny cost estimate first
        try:
            cost = client.metadata.get_cost(
                dataset="GLBX.MDP3",
                symbols=["ES.n.0"],
                stype_in="continuous",
                schema="ohlcv-1d",
                start=(today - timedelta(days=2)).isoformat(),
                end=today.isoformat(),
            )
            out["cost_check_es_1d"] = float(cost)
        except Exception as e:
            out["cost_check_es_1d_error"] = "{}: {}".format(type(e).__name__, e)

        # Test: VX futures daily
        try:
            df = client.timeseries.get_range(
                dataset="XCBF.PITCH",
                symbols=["VX.c.0"],
                stype_in="continuous",
                schema="ohlcv-1d",
                start=(today - timedelta(days=5)).isoformat(),
                end=today.isoformat(),
            ).to_df()
            out["queries"]["vx_daily"] = {
                "ok": True, "rows": len(df) if df is not None else 0,
                "last_close": float(df.iloc[-1]["close"]) if df is not None and not df.empty else None,
            }
        except Exception as e:
            out["queries"]["vx_daily"] = {
                "ok": False,
                "error_type": type(e).__name__,
                "error": str(e)[:300],
            }

        # Test: ES futures daily
        try:
            df = client.timeseries.get_range(
                dataset="GLBX.MDP3",
                symbols=["ES.n.0"],
                stype_in="continuous",
                schema="ohlcv-1d",
                start=(today - timedelta(days=5)).isoformat(),
                end=today.isoformat(),
            ).to_df()
            out["queries"]["es_daily"] = {
                "ok": True, "rows": len(df) if df is not None else 0,
                "last_close": float(df.iloc[-1]["close"]) if df is not None and not df.empty else None,
            }
        except Exception as e:
            out["queries"]["es_daily"] = {
                "ok": False,
                "error_type": type(e).__name__,
                "error": str(e)[:300],
            }

        # Test: SPY definitions (smallest OPRA query)
        try:
            df = client.timeseries.get_range(
                dataset="OPRA.PILLAR",
                symbols=["SPY.OPT"],
                stype_in="parent",
                schema="definition",
                start=(today - timedelta(days=2)).isoformat(),
                end=today.isoformat(),
            ).to_df()
            out["queries"]["spy_definitions"] = {
                "ok": True, "rows": len(df) if df is not None else 0,
            }
        except Exception as e:
            out["queries"]["spy_definitions"] = {
                "ok": False,
                "error_type": type(e).__name__,
                "error": str(e)[:300],
            }

    except ImportError as e:
        out["error"] = "databento SDK not installed: {}".format(e)
    except Exception as e:
        out["error"] = "{}: {}".format(type(e).__name__, e)

    return jsonify(out)


@app.route("/diag")
def diag_endpoint():
    """
    Comprehensive live diagnostic. Runs actual queries against each data
    source and reports what's working vs. broken.
    """
    out = {
        "operating_mode": OPERATING_MODE,
        "modules": {
            "volume_truth":     HAS_VOLUME_TRUTH,
            "safety_gates":     HAS_SAFETY_GATES,
            "regime_filter":    HAS_REGIME,
            "overnight":        HAS_OVERNIGHT,
            "gamma_exposure":   HAS_GEX,
            "new_strategies":   HAS_NEW_STRATS,
            "iv_rank":          HAS_IV_RANK,
        },
        "api_keys": {
            "alpaca":    bool(os.getenv("APCA_API_KEY_ID", "").strip()),
            "databento": bool(os.getenv("DATABENTO_API_KEY", "").strip()),
            "anthropic": bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
            "telegram":  bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip()),
        },
    }

    # --- Databento live tests ---
    db_test = {"sdk_installed": False, "tests": {}}
    try:
        import databento_adapter
        db_test["sdk_installed"] = databento_adapter._SDK_AVAILABLE
        db_test["available"]     = databento_adapter.is_available()

        if databento_adapter.is_available():
            # VIX
            try:
                vix = databento_adapter.get_vix_spot()
                db_test["tests"]["vix"] = {
                    "ok":     vix is not None,
                    "value":  vix,
                }
            except Exception as e:
                db_test["tests"]["vix"] = {"ok": False, "error": str(e)}

            # ES overnight
            try:
                bars = databento_adapter.get_overnight_bars("ES")
                db_test["tests"]["es_overnight"] = {
                    "ok":       len(bars) > 0,
                    "bar_count": len(bars) if bars else 0,
                }
            except Exception as e:
                db_test["tests"]["es_overnight"] = {"ok": False, "error": str(e)}

            # SPY chain
            try:
                chain = databento_adapter.get_options_chain_snapshot("SPY")
                db_test["tests"]["spy_chain"] = {
                    "ok":           len(chain) > 0,
                    "contract_count": len(chain) if chain else 0,
                }
            except Exception as e:
                db_test["tests"]["spy_chain"] = {"ok": False, "error": str(e)}

    except ImportError:
        db_test["import_error"] = "databento_adapter module not loadable"

    out["databento"] = db_test

    # --- Current cached state ---
    with _market_state_lock:
        out["cached_state"] = {
            "regime":          _market_state.get("regime"),
            "gex_bias":        _market_state.get("gex_bias"),
            "premarket_brief": _market_state.get("premarket_brief"),
        }

    return jsonify(out)


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
try:
    import paper_trader
    paper_trader.init_paper_columns(DB_FILE)
    HAS_PAPER_TRADER = True
except Exception as _e:
    HAS_PAPER_TRADER = False
    print("[init] paper_trader init failed: {}".format(_e))
log("DB_FILE: {} | data_dir: {} | RAILWAY_VOLUME_MOUNT_PATH: {}".format(
    DB_FILE, _data_dir,
    os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "(not set)")))
db_load_latest_config()   # restore AI config from last session
_maybe_reset_ai_baseline()  # one-time wipe of pre-reset drifted tuning
threading.Thread(target=background_scheduler, daemon=True).start()
threading.Thread(target=telegram_poller,      daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
