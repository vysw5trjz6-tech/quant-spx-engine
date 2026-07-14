from flask import Flask, jsonify, render_template_string, request, redirect
import requests
import os
import statistics
import math
import threading
import time
import random
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
    import index_data
    HAS_INDEX_DATA = True
except ImportError as _e:
    HAS_INDEX_DATA = False
    print("[init] index_data unavailable: {}".format(_e))

try:
    import index_options
    HAS_INDEX_OPTIONS = True
except ImportError as _e:
    HAS_INDEX_OPTIONS = False
    print("[init] index_options unavailable: {}".format(_e))

try:
    from vol1d import config as vol1d_config
    from vol1d import shadow as vol1d_shadow
    from vol1d import state as vol1d_state
    HAS_VOL1D = True
except ImportError as _e:
    HAS_VOL1D = False
    print("[init] vol1d unavailable: {}".format(_e))

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
    "index_insights":   None,   # {"date", "SPX": {...}, "NDX": {...}} from index_options
    "regime_ts":        0,      # epoch when regime was last refreshed
    "vol1d":            None,   # latest vol1d.state.Vol1DState (updater thread)
    "vol1d_ts":         0,      # epoch when vol1d last computed
    "gex_live":         None,   # intraday index GEX (vol1d.gex_live), ~60s cadence
}
_market_state_lock = threading.Lock()


# =============================================
# APP SETUP
# =============================================

app = Flask(__name__)

SCAN_INTERVAL = 300
ORB_BARS      = 6       # 30 min ORB (6 x 5min bars) - institutional standard

# =============================================
# PRODUCT TIERING
#   INDEX (SPX/NDX)  -> context only, no signal cards (no cash-index options)
#   ETF   (SPY/QQQ)  -> intraday/0DTE (the tradeable SPX/NDX proxies)
# =============================================
ETF_PRODUCTS   = ["SPY", "QQQ"]            # intraday/0DTE
INDEX_PRODUCTS = ["SPX", "NDX"]            # context only (display level + RS)

# Intraday / 0DTE tradeable universe. The engine is focused on 0DTE SPX/NDX
# exposure, traded through the daily-expiry ETF proxies.
INTRADAY_SYMBOLS = list(ETF_PRODUCTS)

# Alias for the aux paths (IV/OI sweeps, dashboards) that iterate the full
# coverage set.
SYMBOLS = list(ETF_PRODUCTS)

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
    # Expectancy floor on alerts: suppress signals whose modeled probability
    # of reaching T1 is below this. ORB t1_prob falls as the opening range
    # widens relative to the average daily range (T1 sits a full extra range
    # away) -- ~1:1 R:R at a 20-30% modeled hit rate is negative EV even at
    # grade B. Signals without a computed probability default to 50 (pass).
    "alert_min_t1_prob":        40,
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

    # --- Clear-air (resistance/support proximity) cap ---
    # Levels within this % of current price are treated as already tested /
    # within noise and do NOT block the path to T1. Without this, an intraday
    # 1H swing-high sitting fractions of a percent overhead caps nearly every
    # signal at C. A weak (1H) blocking level applies a softer one-grade
    # penalty rather than the hard C cap reserved for 4hr/daily levels.
    "clear_air_tol_pct":        0.12,
    "clear_air_weak_strength":  1,
    # Chase-into-resistance guard: when a level blocks the path to T1 and sits
    # within this fraction of the distance to T1 (i.e. right on top of entry),
    # the trade has to punch through overhead supply immediately. Demote such
    # setups to D so they're dropped from the board rather than shown/alerted.
    "chase_resist_frac":        0.34,
    # Overpriced-option guardrail: skip a setup when the selected (~0.40 delta)
    # contract's implied vol exceeds this. Rich IV means paying up for a move
    # the market has already priced in.
    "max_option_iv":            0.70,

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
# 2026-07-09: floor bumped for the stats-labeling fixes. Trades before this
# carried fabricated AI labels -- paper rows had no entry_hour/rs/alignment
# (defaulted to 9:30-10:00 / rs_negative / aligned) and weekly swing trades
# had no grade at all (bucketed as "?"), which produced insights like
# '"?" outperforms every grade' and 'zero win rate after 11 AM'.
AI_TRAINING_FLOOR = "2026-07-09T00:00:00+00:00"

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


def _storage_status():
    """Inspect where data_path() resolves and whether it actually persists.

    Returns a dict the boot sequence and /diagnostic both consume:
      persistent : bool  -- True when DATA_DIR or a Railway volume is mounted
      writable   : bool  -- the dir exists and we can create a file in it
      data_dir   : str   -- the resolved base directory
      source     : str   -- which env var supplied it (or "/tmp fallback")
      detail     : str   -- human-readable note for the unhappy paths

    `persistent` being False means every SQLite DB (volume profiles, but also
    the paid Databento IV/OI history) lives on the container's ephemeral disk
    and is wiped on the next redeploy -- the exact failure that silently reset
    volume profiles to 0 each boot."""
    if os.getenv("DATA_DIR"):
        source = "DATA_DIR"
    elif os.getenv("RAILWAY_VOLUME_MOUNT_PATH"):
        source = "RAILWAY_VOLUME_MOUNT_PATH"
    else:
        source = "/tmp fallback"
    persistent = source != "/tmp fallback"

    writable = False
    detail   = ""
    try:
        os.makedirs(_data_dir, exist_ok=True)
        probe = _data_dir.rstrip("/") + "/.storage_probe"
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
        writable = True
    except Exception as e:
        detail = "data dir not writable: {}".format(e)

    if not persistent:
        detail = ("ephemeral storage -- all SQLite DBs (incl. paid Databento "
                  "IV/OI history) are WIPED on every redeploy; attach a Railway "
                  "volume or set DATA_DIR")
    elif not writable and not detail:
        detail = "persistent path configured but not writable"

    return {
        "persistent": persistent,
        "writable":   writable,
        "data_dir":   _data_dir,
        "source":     source,
        "detail":     detail,
    }


def _log_storage_status():
    """Boot-time announcement of persistence health. Ephemeral or unwritable
    storage is a data-loss condition, so it's logged at WARN/ERROR rather than
    buried in an info line that nobody reads."""
    st = _storage_status()
    if not st["persistent"]:
        log_warn("storage.ephemeral | data_dir={} | {}".format(
            st["data_dir"], st["detail"]))
    elif not st["writable"]:
        log_error("storage.not_writable | data_dir={} | {}".format(
            st["data_dir"], st["detail"]))
    else:
        log("storage.persistent | data_dir={} (via {})".format(
            st["data_dir"], st["source"]))
    return st


def _databento_blocked():
    """True when the Databento billing breaker is currently engaged."""
    try:
        import databento_adapter as _da
        return bool(_da.billing_status().get("blocked"))
    except Exception:
        return False


def _absorb_adapter_log(line):
    """Sink for databento_adapter._emit_error lines. The adapter writes its
    errors (notably databento.billing_blocked) to stderr only; mirroring
    them into the debug_log ring makes the breaker's root cause visible on
    /debug next to the gex/oi symptoms it explains. No re-print here --
    the adapter already wrote the line to stderr."""
    with state_lock:
        debug_log.append(line)
        if len(debug_log) > 150:
            debug_log.pop(0)


try:
    import databento_adapter as _da_mirror
    _da_mirror.register_log_mirror(_absorb_adapter_log)
except Exception:
    pass


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
                          ("mode","TEXT"), ("paper_key","TEXT")]:
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
    AI must learn from every signal it suggested. The one exclusion is
    mode='paper_t2': the paper trader's T2 walk is target-calibration
    telemetry, not a second trade -- counting it would hand every signal
    an extra, much-harder row and deflate every win-rate the AI sees.
    P&L is intentionally
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
                   signal_type, horizon
            FROM trades
            WHERE outcome != 'OPEN' AND ts >= ?
              AND COALESCE(mode, '') != 'paper_t2'
            ORDER BY ts DESC
        """, (epoch,))
        rows = c.fetchall()
        conn.close()
        cols = ["symbol","direction","outcome","r_mult",
                "grade","grade_pts","gap_pct","gap_dir","rs","entry_hour","ts",
                "signal_type","horizon"]
        return [dict(zip(cols, r)) for r in rows]
    except Exception as e:
        log("DB get all closed trades error: {}".format(e))
        return []


# --- vol1d journaling (spec §6) ---------------------------------------------
# Entry/exit rows carry the vol regime they fired into so P&L can later be
# bucketed by combined_regime and the fade/trend routing checked. Columns
# are added via the same idempotent ALTER TABLE migration pattern
# paper_trader uses.

_VOL1D_ENTRY_COLS = ("vix1d", "vix1d_tod_z", "exp_move_adj", "iv_rv_spread",
                     "vol_state", "combined_regime", "vol1d_spiking",
                     "vol1d_residual", "vol1d_confidence")
_VOL1D_EXIT_COLS  = ("exit_vix1d", "exit_vix1d_tod_z", "exit_vol_state",
                     "exit_combined_regime", "exit_vol1d_spiking")


def init_vol1d_journal_columns():
    """Idempotent migration: vol1d entry fields on signals + trades, exit
    fields on trades."""
    conn = db_utils.connect(DB_FILE)
    types = {"vol_state": "TEXT", "combined_regime": "TEXT",
             "exit_vol_state": "TEXT", "exit_combined_regime": "TEXT",
             "vol1d_spiking": "INTEGER", "exit_vol1d_spiking": "INTEGER"}
    for table, cols in (("signals", _VOL1D_ENTRY_COLS),
                        ("trades",  _VOL1D_ENTRY_COLS + _VOL1D_EXIT_COLS)):
        for col in cols:
            try:
                conn.execute("ALTER TABLE {} ADD COLUMN {} {}".format(
                    table, col, types.get(col, "REAL")))
            except Exception:
                pass   # column already exists
    conn.commit()
    conn.close()


def _vol1d_entry_fields():
    """Current Vol1DState flattened onto the entry journal columns, or {}
    when the module is off/warming — journaling must never block a trade."""
    if not HAS_VOL1D:
        return {}
    st = get_vol1d_state()
    if st is None:
        return {}
    return {
        "vix1d":            st.vix1d,
        "vix1d_tod_z":      st.vix1d_tod_z,
        "exp_move_adj":     st.exp_move_adj,
        "iv_rv_spread":     st.iv_rv_spread,
        "vol_state":        st.vol_state,
        "combined_regime":  st.combined_regime,
        "vol1d_spiking":    1 if st.spiking else 0,
        "vol1d_residual":   st.qa_residual,
        "vol1d_confidence": st.confidence,
    }


def _vol1d_stamp_row(conn, table, row_id, fields):
    if not fields or row_id is None:
        return
    sets = ", ".join("{}=?".format(k) for k in fields)
    conn.execute("UPDATE {} SET {} WHERE id=?".format(table, sets),
                 list(fields.values()) + [row_id])


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
        _vol1d_stamp_row(conn, "signals", c.lastrowid, _vol1d_entry_fields())
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
        # Live entries carry the vol regime they fired into. Paper-replay
        # rows are inserted at EOD when the state no longer reflects entry
        # time — they inherit entry-time fields from their signal row.
        if mode != "paper" and mode != "paper_t2":
            _vol1d_stamp_row(conn, "trades", trade_id, _vol1d_entry_fields())
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
        # Exit-time vol regime (spec §6: log at entry AND exit).
        entry = _vol1d_entry_fields()
        if entry:
            _vol1d_stamp_row(conn, "trades", trade_id, {
                "exit_vix1d":            entry["vix1d"],
                "exit_vix1d_tod_z":      entry["vix1d_tod_z"],
                "exit_vol_state":        entry["vol_state"],
                "exit_combined_regime":  entry["combined_regime"],
                "exit_vol1d_spiking":    entry["vol1d_spiking"],
            })
        conn.commit()
        conn.close()
        log("Trade {} closed: {} pnl={}".format(trade_id, outcome, pnl))
    except Exception as e:
        log("DB close trade error: {}".format(e))


def db_get_today_trades():
    try:
        # Trade `ts` is stored in UTC (db_log_trade), so "today's" trades must
        # be bounded by the ET session day expressed in UTC -- not matched with
        # `ts LIKE <ET-date>%`. Those agree only during RTH; once it's past
        # 8 PM ET (>= midnight UTC) the UTC date is already tomorrow and a
        # LIKE on the ET date silently drops every trade entered that session.
        et       = pytz.timezone("America/New_York")
        start_et = datetime.now(et).replace(hour=0, minute=0, second=0,
                                            microsecond=0)
        start_utc = start_et.astimezone(pytz.utc).isoformat()
        end_utc   = (start_et + timedelta(days=1)).astimezone(pytz.utc).isoformat()
        conn  = db_utils.connect(DB_FILE)
        c     = conn.cursor()
        c.execute("""
            SELECT id,symbol,direction,premium,contracts,stop,target,
                   outcome,exit_price,pnl,r_mult,ts
            FROM trades WHERE ts >= ? AND ts < ?
            ORDER BY ts DESC
        """, (start_utc, end_utc))
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
# occupies a slot in its tier. INTRADAY holds ONE live position at a time.
# (WEEKLY is a legacy tier: the swing engine is gone, but the monitor still
# resolves any WEEKLY rows left open in the trades DB.) While a tier is full the scanner
# emits no new alerts in it. The live monitor resolves auto positions against
# the UNDERLYING targets (T1 = win, stop = loss) each scan, then frees the
# slot. Manually-taken trades (mode != 'auto') also occupy a slot so they
# mute alerts, but are only closed by the user.

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
    """True if the given tier (e.g. INTRADAY) already holds a live position.

    WEEKLY is a legacy horizon: the swing/weekly engine was removed, but the
    monitor still resolves any WEEKLY rows left open in the trades DB.
    """
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
    WEEKLY:   full daily bars for days AFTER the entry date, plus intraday
              bars at/after the entry timestamp (covers the entry day's
              post-entry tape and today's session).
    Falls back to the current price when no post-entry bars exist yet.

    Bars are stamped at bar START, so only bars beginning at/after entry
    qualify. Never widen to pre-entry bars: an ORB breakout's stop sits
    inside the morning's range by construction, so including the session
    history reads the stop as already "touched" and force-closes every
    position as a LOSS one scan after entry.
    """
    symbol = pos["symbol"]
    hi = lo = None
    try:
        entry_ts = pos.get("ts") or ""
        if pos.get("horizon") == "WEEKLY":
            daily = get_daily(symbol) or []
            entry_day = entry_ts[:10]
            rel = [b for b in daily if (b.get("t") or "")[:10] > entry_day]
            rel += [b for b in (get_intraday(symbol) or [])
                    if (b.get("t") or "") >= entry_ts]
        else:
            intra = get_intraday(symbol) or []
            rel = [b for b in intra if (b.get("t") or "") >= entry_ts]
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
import bar_utils


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


# SPX/NDX have no live tradable feed here (Alpaca carries ETF options only).
# Levels are anchored to the REAL index prior close (index_data) and moved by
# the ETF proxy's intraday return; the fixed multipliers remain only as the
# last-resort fallback when no index source is reachable.
INDEX_PROXY = {
    "SPX": ("SPY", 10.0),    # SPX ~= SPY x 10 (fallback multiplier only)
    "NDX": ("QQQ", 41.0),    # NDX ~= QQQ x ~41 (fallback multiplier only)
}


def _proxy_prev_close(proxy):
    """Last COMPLETED daily close for the proxy ETF (skips today's live bar)."""
    bars = get_daily(proxy)
    if not bars:
        return None
    et = pytz.timezone("America/New_York")
    today_iso = datetime.now(et).date().isoformat()
    for b in reversed(bars):
        if str(b.get("t", ""))[:10] < today_iso:
            return b.get("c")
    return None


def _index_anchor_level(idx, proxy, proxy_last):
    """Real prior index close moved by the proxy's return since ITS close.

    level = real_prev_close x (proxy_last / proxy_prev_close). Both legs
    span the same period (prior close -> now), so the ratio carries gaps and
    folds the ETF's basis drift out; a fixed multiplier can be tens of SPX
    points off. Returns (level, anchored: bool) -- anchored False means the
    multiplier fallback is needed.
    """
    if not HAS_INDEX_DATA or not proxy_last:
        return None, False
    try:
        prev = index_data.prev_session(idx)
        p_prev = _proxy_prev_close(proxy)
        if not prev or not p_prev:
            return None, False
        return round(prev["c"] * (proxy_last / p_prev), 2), True
    except Exception:
        return None, False


def index_context():
    """Display-only SPX/NDX context: real-anchored level + intraday % + RS.

    Never produces a tradable signal. Direction/change still comes from the
    liquid SPY/QQQ proxies; the LEVEL is anchored on the actual index close
    when index_data has one.
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
            level, anchored = _index_anchor_level(idx, proxy, px)
            if level is None:
                level = round(px * factor, 2) if px else None
            out.append({
                "symbol":   idx,
                "proxy":    proxy,
                "level":    level,
                "pct":      chg,
                "rs":       relative_strength(chg, spy_chg),
                "is_proxy": not anchored,
                "anchored": anchored,
            })
        except Exception:
            out.append({"symbol": idx, "proxy": proxy, "level": None,
                        "pct": None, "rs": None, "is_proxy": True,
                        "anchored": False})
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
            px=("{} anchored".format(c.get("proxy", "-"))
                 if c.get("anchored") else c.get("proxy", "-")),
        )
    all_anchored = all(c.get("anchored") for c in ctx)
    footer = ("(real index close, moved by proxy &mdash; context only)"
              if all_anchored else
              "(proxy levels &mdash; context only, trade SPY/QQQ)")
    return (
        "<div style='background:#0d1117;border:1px solid #30363d;border-radius:8px;"
        "padding:8px 12px;margin-bottom:10px;font-size:11px'>"
        "<span style='color:#8b949e;text-transform:uppercase;letter-spacing:.6px;"
        "font-size:9px;font-weight:700;margin-right:12px'>Index Context</span>"
        + cells +
        "<span style='color:#6e7681;font-size:9px;margin-left:6px'>"
        + footer + "</span></div>"
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
    score = max(0.0, min(100.0, score * vol_mult))

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
    score = max(0.0, min(100.0, score * vol_mult))

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
    score = max(0.0, min(100.0, score * vol_mult))

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
    # "Yesterday's close" must come from a bar dated BEFORE today's session:
    # during RTH the daily series ends with today's still-forming bar, whose
    # close is just the latest price -- using it made the "gap" the inverse of
    # the intraday move (every selloff name showed a phantom gap UP).
    today_key  = (intraday_bars[0].get("t") or "")[:10]
    prev_close = None
    for b in reversed(daily_bars):
        if (b.get("t") or "")[:10] < today_key:
            prev_close = b["c"]
            break
    today_open = intraday_bars[0]["o"]
    if not prev_close:
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
        # Unknown structure is NEUTRAL, not bullish: clear_to_t1/t2 stay True
        # so the signal isn't demoted, but no_data tells the rank score and
        # grade bump to skip the clear-air credit -- an empty level list
        # (data gap) must not outrank a genuinely clear path.
        return {"clear_to_t1": True, "clear_to_t2": True, "no_data": True,
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


def _blocker_chase_frac(price, t1, clear_air):
    """Fraction of the path from entry to T1 that the nearest blocking level
    sits at: ~0 means the blocker is right on top of entry (worst -- the trade
    must punch through overhead supply immediately), ~1 means it's just shy of
    T1. Returns None when nothing blocks T1 or the inputs are missing."""
    if not clear_air or clear_air.get("clear_to_t1") or not t1 or not price:
        return None
    blk = (clear_air.get("blocking_level") or {})
    blk_px = blk.get("price")
    if not blk_px:
        return None
    path = abs(t1 - price)
    if path <= 0:
        return None
    return abs(blk_px - price) / path


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

# Symbols already warned about a suspect bar-of-day ratio this process,
# so a persistent feed mismatch logs once per symbol instead of per scan.
_tv_suspect_warned = set()

# Aggregate feed-mismatch health. A handful of suspect symbols is noise, but a
# large slice of the universe tripping the detector means volume_profile.db was
# built on a different feed than the live bars -- every volume gate is then
# silently running on the (biased) fallback. We surface the count in
# /diagnostic and, past a threshold, fire one aggregate warning + a single
# same-day auto-rebuild so the degradation self-heals instead of persisting.
_tv_suspect_today = {"date": None, "symbols": set(), "rebuilt": False}
_tv_suspect_lock  = threading.Lock()
_TV_SUSPECT_REBUILD_THRESHOLD = 8

# Empirical intraday volume distribution. US equity volume is U-shaped:
# heaviest in the opening drive, a midday trough, a secondary bump into the
# close. The legacy fallback below (used when volume_truth has no profile, or
# when a feed mismatch makes the profile ratio untrustworthy) modeled this as a
# single power law (day_fraction ** 0.7) -- monotonic, so it can't represent
# the U: it understates a noon ratio and overstates a 9:35 one, biasing every
# volume gate exactly when we're already degraded. Replace it with a normalized
# per-bar weight curve: weight[i] is bar i's expected share of the day's volume,
# summing to 1 across the 78 RTH 5-min slots.
_RTH_BARS = 78


def _build_intraday_vol_weights(n=_RTH_BARS):
    open_amp,  open_tau  = 2.4, 6.0     # opening drive decays over ~30 min
    close_amp, close_tau = 1.3, 9.0     # softer ramp into the close
    raw = []
    for i in range(n):
        raw.append(1.0
                   + open_amp  * math.exp(-i / open_tau)
                   + close_amp * math.exp(-(n - 1 - i) / close_tau))
    total = sum(raw) or 1.0
    return [w / total for w in raw]


_INTRADAY_VOL_WEIGHTS = _build_intraday_vol_weights()


def _expected_bar_vol_fraction(bar_idx):
    """Expected share of a day's volume for 5-min bar `bar_idx` (0 = 9:30)."""
    if not _INTRADAY_VOL_WEIGHTS:
        return 1.0 / _RTH_BARS
    i = max(0, min(bar_idx, len(_INTRADAY_VOL_WEIGHTS) - 1))
    return _INTRADAY_VOL_WEIGHTS[i]


def _note_tv_suspect(symbol):
    """Record a suspect bar-of-day ratio for `symbol`. When the distinct count
    crosses the threshold, warn loudly once and kick a single same-day rebuild
    of every volume profile (feed-mismatch recovery)."""
    today = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
    trip_rebuild = False
    with _tv_suspect_lock:
        if _tv_suspect_today["date"] != today:
            _tv_suspect_today["date"]    = today
            _tv_suspect_today["symbols"] = set()
            _tv_suspect_today["rebuilt"] = False
        _tv_suspect_today["symbols"].add(symbol)
        count = len(_tv_suspect_today["symbols"])
        if (count >= _TV_SUSPECT_REBUILD_THRESHOLD
                and not _tv_suspect_today["rebuilt"]):
            _tv_suspect_today["rebuilt"] = True
            trip_rebuild = True
    if trip_rebuild:
        log_warn(("Volume feed mismatch: {} symbols tripped the suspect "
                  "bar-of-day detector today -- volume_profile.db looks built "
                  "on a different feed than the live bars. Forcing a profile "
                  "rebuild; volume gates run on the fallback until it "
                  "completes.").format(count))
        if HAS_VOLUME_TRUTH:
            threading.Thread(target=_force_rebuild_volume_profiles,
                             daemon=True).start()


def _force_rebuild_volume_profiles():
    """Rebuild every volume profile regardless of age (feed-mismatch recovery).
    build_profile bypasses the 24h needs_refresh() age gate."""
    if not HAS_VOLUME_TRUTH:
        return
    try:
        all_syms = list(SYMBOLS)
        built = sum(1 for s in all_syms if volume_truth.build_profile(s))
        log("Forced volume-profile rebuild: {}/{} symbols".format(
            built, len(all_syms)))
    except Exception as e:
        log("forced volume profile rebuild error: {}".format(e))


def volume_health():
    """Feed-mismatch health snapshot for /diagnostic."""
    with _tv_suspect_lock:
        return {
            "date":            _tv_suspect_today["date"],
            "suspect_count":   len(_tv_suspect_today["symbols"]),
            "suspect_symbols": sorted(_tv_suspect_today["symbols"]),
            "auto_rebuilt":    _tv_suspect_today["rebuilt"],
            "rebuild_threshold": _TV_SUSPECT_REBUILD_THRESHOLD,
        }


def _time_vol_ratio_suspect(ratio, intraday_today, current_bar_idx):
    """A real volume event shows up bar-over-bar as well as against the
    historical slot median. A huge slot ratio (>10x) on a bar that is NOT
    even 2x its neighbor is the signature of the profile being built on a
    different (thinner) feed than the live bars -- e.g. IEX medians vs
    consolidated SIP volume -- not of real flow."""
    if ratio <= 10.0 or current_bar_idx < 1:
        return False
    prev_vol = intraday_today[current_bar_idx - 1].get("v") or 0
    cur_vol  = intraday_today[current_bar_idx].get("v") or 0
    return prev_vol > 0 and cur_vol / prev_vol < 2.0


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
                if _time_vol_ratio_suspect(ratio, intraday_today, current_bar_idx):
                    if symbol not in _tv_suspect_warned:
                        _tv_suspect_warned.add(symbol)
                        log_event("volume_truth.ratio_suspect", level="warn",
                                  symbol=symbol, ratio=ratio,
                                  bar_idx=current_bar_idx,
                                  hint="profile feed mismatch? rebuild volume_profile.db")
                    # Track aggregate health so a universe-wide feed mismatch
                    # gets one loud warning + an auto-rebuild, not just N quiet
                    # per-symbol logs.
                    _note_tv_suspect(symbol)
                    # Fall through to the legacy approximation, which is
                    # internally consistent (same feed for bar and average).
                else:
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

    # Expected single-bar volume from the U-shaped intraday distribution
    # (open/lunch/close), not a monotonic power law that can't see the humps.
    expected_per_bar = avg_daily_vol * _expected_bar_vol_fraction(current_bar_idx)
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

    # Clear air (no_data = level lookup came back empty: neutral, no credit)
    ca = result.get("clear_air") or {}
    if ca.get("no_data"):
        pass
    elif ca.get("clear_to_t2"):
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
    SPY/QQQ have daily options.
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
                      et_hour=None, zero_dte_cutoff=None, max_iv=None):
    """
    Fetch the nearest available ATM option via Alpaca options snapshot API.

    SPY/QQQ have daily 0DTE options.
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

            # Overpriced-option guardrail: the selected (~0.40 delta) contract
            # is representative of the chain's vol. If its IV tops the cap, the
            # move is already priced in -- skip rather than pay up. The "IV_HIGH"
            # sentinel lets the caller keep the row on the board as WATCHING.
            if max_iv and best.get("iv") and best["iv"] > max_iv:
                log("  {} {} skipped: IV {:.0f}% > {:.0f}% cap (overpriced)".format(
                    symbol, option_type, best["iv"] * 100, max_iv * 100))
                return None, None, False, "IV_HIGH"

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
                    chase_frac = _blocker_chase_frac(price, result.get(t1_key), clear_air)
                    if (chase_frac is not None
                            and chase_frac <= cfg.get("chase_resist_frac", 0.34)):
                        # Chasing into immediate resistance -- hard-demote to D.
                        grade = "D"; grade_color = "#8b949e"
                        grade_pts = min(grade_pts, cfg["grade_c_min"] - 1)
                        log("  {} chase-into-resistance -> D ({} {:.2f}, {:.0f}% of path to T1)".format(
                            symbol, blk.get("label", "level"), blk.get("price", 0),
                            chase_frac * 100))
                    else:
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
                    zero_dte_cutoff=cfg.get("zero_dte_cutoff_hour", 14.5),
                    max_iv=cfg.get("max_option_iv"))

                if premium and is_live:
                    stp, tgt = option_risk_levels(premium)
                    result["premium"]   = round(premium, 2)
                    result["strike"]    = strike
                    result["stop"]      = stp
                    result["target"]    = tgt
                    result["dte_label"] = dte_label or "0DTE"
                    result["status"]    = "SIGNAL"
                elif dte_label == "IV_HIGH":
                    # Valid setup, but the option is too richly priced to trade.
                    result["status"]      = "WATCHING"
                    result["skip_reason"] = "IV>{:.0f}% (overpriced)".format(
                        (cfg.get("max_option_iv") or 0.70) * 100)
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
        # Quality score on a 0-100 scale, consistent with the alt-strategy
        # signals (VWAP_TREND / Keltner / IB-extension) so `score` is
        # comparable across signal types. Built from breakout extension +
        # relative volume; the old raw formula produced ~2 here vs ~70 there.
        score = (50.0
                 + min(breakout_strength * 100.0 * 6.0, 30.0)
                 + min(vol_ratio * 12.0, 20.0))
        score = max(0.0, min(100.0, score * vol_mult))

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
            chase_frac = _blocker_chase_frac(price, result.get(t1_key), clear_air)
            if (chase_frac is not None
                    and chase_frac <= cfg.get("chase_resist_frac", 0.34)):
                # Blocker sits right on top of entry -- chasing into immediate
                # resistance. Hard-demote to D so it drops off the board.
                grade = "D"; grade_color = "#8b949e"
                grade_pts = min(grade_pts, cfg["grade_c_min"] - 1)
                log("  {} chase-into-resistance -> D ({} {:.2f}, {:.0f}% of path to T1)".format(
                    symbol, blk.get("label", "level"), blk.get("price", 0),
                    chase_frac * 100))
            else:
                weak = blk.get("strength", 3) <= cfg.get("clear_air_weak_strength", 1)
                if weak:
                    # A lone 1H swing-high is not strong enough for the hard C
                    # cap. Apply a softer one-grade penalty and higher ceiling.
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
        elif (clear_air["clear_to_t2"] and not clear_air.get("no_data")
                and grade_pts >= 70):
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
            zero_dte_cutoff=cfg.get("zero_dte_cutoff_hour", 14.5),
            max_iv=cfg.get("max_option_iv"))

        if premium and is_live:
            stp, tgt = option_risk_levels(premium)
            result["premium"]   = round(premium, 2)
            result["strike"]    = strike
            result["stop"]      = stp
            result["target"]    = tgt
            result["dte_label"] = dte_label or "0DTE"
            result["status"]    = "SIGNAL"
        elif dte_label == "IV_HIGH":
            # Valid setup, but the option is too richly priced to trade.
            result["status"]      = "WATCHING"
            result["skip_reason"] = "IV>{:.0f}% (overpriced)".format(
                (cfg.get("max_option_iv") or 0.70) * 100)
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
    _min_t1_prob  = _cfg.get("alert_min_t1_prob", 40)
    alertable = [s for s in signals
                 if s.get("grade") in _allowed_gr and s.get("aligned", True)
                 and (s.get("t1_prob") if s.get("t1_prob") is not None else 50)
                 >= _min_t1_prob]
    if signals and not alertable:
        log("Intraday: {} signal(s) below alert floor {}, t1_prob<{}%, or "
            "counter-trend; no alert".format(
                len(signals), _alert_floor, _min_t1_prob))

    # One live INTRADAY position at a time: if the tier is occupied, emit no new
    # intraday alerts until it closes (win/loss on underlying targets).
    intraday_locked = tier_has_open_position("INTRADAY")
    if intraday_locked:
        log("INTRADAY tier locked -- live position open, suppressing alerts")

    # vol1d SHADOW hook: evaluate + log what the vol module WOULD do to
    # each alertable signal (veto/stand-aside/downweight/size). It filters
    # NOTHING until vol1d config enforce=True is flipped after reviewing
    # the vol1d_shadow logs.
    if HAS_VOL1D and alertable:
        try:
            _vcfg = vol1d_config.get_config()
            alertable = vol1d_shadow.process_signals(
                alertable, get_vol1d_state(), cfg=_vcfg,
                has_open_position=intraday_locked)
            if _vcfg.get("enforce"):
                log_event("vol1d.enforce_active", allowed=len(alertable))
        except Exception as _e:
            log("vol1d shadow error: {}".format(_e))

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

            msg = (
                "{sig_type} {horizon} SIGNAL — {grade} ({gpts}pts)\n\n"
                "{sym} {dirn}  •  ${price}\n"
                "Strike: {strike}  •  Premium: ${prem}\n"
                "Stop ${stop}  •  Target ${target}  •  conv x{conv:.2f}\n\n"
                "Gap: {gap}%  •  RS vs SPY: {rs}%\n"
                "Vol: {vol_lbl}{ctx}"
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
        # Unrecorded labels go to explicit "unknown" buckets instead of being
        # defaulted (entry_hour->9.5, rs->0, aligned->True). Paper trades had
        # none of these fields, so the defaults silently rewrote most of the
        # dataset: every paper row showed up as a 9:30-10:00, rs_negative,
        # aligned trade and the AI tuned time/RS/alignment filters on fiction.
        "by_time":       _wl_groups(trades, lambda t: _time_bucket(t["entry_hour"])
                                    if t.get("entry_hour") is not None else "unknown"),
        "by_signal":     _wl_groups(trades, lambda t: str(t.get("signal_type") or "?")),
        "by_rs":         _wl_groups(trades, lambda t: "rs_unknown" if t.get("rs") is None
                                    else ("rs_positive" if t["rs"] > 0 else "rs_negative")),
        "by_alignment":  _wl_groups(trades, lambda t: "unknown" if t.get("aligned") is None
                                    else ("aligned" if t["aligned"] else "counter_trend")),
        # WEEKLY swing trades and INTRADAY 0DTE trades are different
        # strategies with different hold times -- surface the split so
        # cross-horizon comparisons are visible instead of implicit.
        "by_horizon":    _wl_groups(trades, lambda t: str(t.get("horizon") or "?")),
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
- by_horizon separates WEEKLY swing trades (multi-day holds) from INTRADAY
  0DTE trades. These are DIFFERENT STRATEGIES: never compare grades, time
  buckets, or win rates across horizons, and never conclude one horizon's
  signals "outperform" another's grades.
- Groups named "unknown", "rs_unknown", or "?" mean the label was not
  recorded for those trades. They are missing data, not a signal category:
  never base a change on them and never recommend targeting them.
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

    prompt = """You are reviewing a week of automated 0DTE options signals from an algorithmic trading system. \
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
                # Real index prior-session bars so the brief can speak in
                # actual SPX/NDX points instead of ETF-proxy dollars.
                spx_prev = ndx_prev = None
                if HAS_INDEX_DATA:
                    try:
                        spx_prev = index_data.prev_session("SPX")
                        ndx_prev = index_data.prev_session("NDX")
                    except Exception as e:
                        log("index data fetch error: {}".format(e))
                brief = overnight_context.get_premarket_brief(
                    prev_rth_close = prev["c"],
                    prev_rth_high  = prev["h"],
                    prev_rth_low   = prev["l"],
                    rth_open       = rth_open,
                    spx_prev       = spx_prev,
                    ndx_prev       = ndx_prev,
                )
                with _market_state_lock:
                    _market_state["premarket_brief"] = brief
                log("Premarket brief refreshed")
    except Exception as e:
        log("premarket brief refresh error: {}".format(e))

    try:
        if HAS_INDEX_OPTIONS:
            _refresh_index_insights()
    except Exception as e:
        log("index options insights error: {}".format(e))

    try:
        if HAS_GEX:
            bias = gamma_exposure.get_gex_bias("SPY")
            with _market_state_lock:
                _market_state["gex_bias"] = bias
            log("GEX bias: {} ({})".format(
                bias.get("tape_bias"), bias.get("note", "")))
    except Exception as e:
        log("GEX bias load error: {}".format(e))


def _refresh_index_insights():
    """
    Once-daily SPX/NDX options insights (0DTE expected move, gamma walls,
    P/C OI) from the real index chains. The chain is EOD data — OI and
    settlement prices only change overnight — so recomputing within a day
    would re-bill the identical Databento pull for the identical answer.
    """
    et    = pytz.timezone("America/New_York")
    today = datetime.now(et).date().isoformat()

    with _market_state_lock:
        existing = _market_state.get("index_insights")
        brief    = _market_state.get("premarket_brief")
    if existing and existing.get("date") == today and (
            existing.get("SPX") or existing.get("NDX")):
        return

    insights = {"date": today}
    for idx, ctx_key in (("SPX", "spx"), ("NDX", "ndx")):
        # Best premarket spot: the futures-implied open in index points;
        # fall back to the real prior close.
        spot = None
        ctx = (brief or {}).get(ctx_key) or {}
        spot = ctx.get("implied_open") or ctx.get("prev_close")
        if not spot and HAS_INDEX_DATA:
            try:
                prev = index_data.prev_session(idx)
                spot = prev["c"] if prev else None
            except Exception:
                spot = None
        if not spot:
            continue
        try:
            ins = index_options.get_index_options_insights(idx, spot)
            if ins:
                insights[idx] = ins
                log("Index options insights built: {} (0DTE EM ±{} pts)".format(
                    idx, ins.get("expected_move_pts")))
        except Exception as e:
            log("index insights error for {}: {}".format(idx, e))

    if insights.get("SPX") or insights.get("NDX"):
        with _market_state_lock:
            _market_state["index_insights"] = insights


def _refresh_volume_profiles():
    """Once-daily rebuild of bar-of-day volume profiles."""
    if not HAS_VOLUME_TRUTH:
        return
    try:
        all_syms = list(SYMBOLS)
        built, failed = volume_truth.refresh_all(all_syms)
        log("Volume profiles refreshed for {} symbols".format(len(built)))
        if failed:
            # A symbol without a profile gets the N/A volume sentinel,
            # which permanently blocks its volume-gated strategies --
            # name the casualties instead of hiding them in a count.
            log_warn("Volume profile build FAILED for {}: {}".format(
                len(failed), ", ".join(sorted(failed))))
    except Exception as e:
        log("volume profile refresh error: {}".format(e))


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


# Re-attempt throttle for the EOD Databento sweeps (GEX/OI). The persistent
# success marker above governs "already done"; this only spaces out RETRIES of
# a sweep that came back empty -- e.g. an OPRA EOD `statistics` batch that
# isn't published yet -- so we recover the same evening without launching a
# thread on every scheduler tick. Module-level so it survives across loop
# iterations; naturally re-arms next day once the stored timestamp is in the
# past and the (date-keyed) success marker no longer matches.
_sweep_retry_at = {}


def _sweep_retry_due(key, gap_min=30):
    """Claim a retry slot for `key`. Returns True at most once per `gap_min`
    minutes, recording the next-eligible time as it does so."""
    now = time.time()
    if now < _sweep_retry_at.get(key, 0.0):
        return False
    _sweep_retry_at[key] = now + gap_min * 60
    return True


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
            # The breaker can trip mid-run (the previous symbol's pull hit
            # a 402/403). Stop here: every further call is dead on arrival
            # and a per-symbol snapshot_empty would bury the real cause.
            if _databento_blocked():
                import databento_adapter as _da
                log_event("gex.snapshot_deferred", level="warn",
                          reason="databento_billing_blocked_midrun",
                          until=_da.billing_status().get("blocked_until"))
                break
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
            elif _databento_blocked():
                # This symbol's own pull tripped the breaker: the chain
                # isn't empty, the account is (out of credit / locked).
                import databento_adapter as _da
                log_event("gex.snapshot_deferred", level="warn", symbol=sym,
                          reason="databento_billing_blocked_midrun",
                          until=_da.billing_status().get("blocked_until"))
                break
            else:
                # Diagnostic: probe the chain directly so the log says
                # which leg was empty (chain itself, vs. compute step
                # downstream). Pass with_price=True so this reuses the exact
                # cache key refresh_gex just populated (the GEX path always
                # pulls with prices); with_price=False would be a *different*
                # cache key and re-issue a second, separately-billed pull.
                import databento_adapter as _da
                chain_probe = _da.get_options_chain_snapshot(
                    sym, with_price=True) or []
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

    all_syms = list(SYMBOLS)
    ok_count      = 0
    empty_count   = 0   # chain pull returned nothing
    save_fails    = 0   # chain arrived but no rows persisted
    exc_count     = 0
    blocked_after = None  # symbols never attempted: breaker tripped mid-loop
    for i, sym in enumerate(all_syms):
        # Billing breaker can trip on any pull in this loop; once it does,
        # the remaining symbols would all come back empty. Bail with the
        # cause instead of silently iterating to an unexplained "0/72".
        if _databento_blocked():
            blocked_after = len(all_syms) - i
            import databento_adapter as _da
            log_event("oi.snapshot_deferred", level="warn",
                      reason="databento_billing_blocked_midrun",
                      skipped_symbols=blocked_after,
                      until=_da.billing_status().get("blocked_until"))
            break
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
                    save_fails += 1
            else:
                empty_count += 1
        except Exception as e:
            exc_count += 1
            if exc_count <= 3:  # only log first few errors
                log("OI snapshot {} error: {}".format(sym, e))

    log("OI snapshots: {}/{} symbols stored "
        "(empty={} save_fail={} errors={} blocked_skipped={})".format(
            ok_count, len(all_syms), empty_count, save_fails, exc_count,
            blocked_after or 0))

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
    First attempted at 10:05 AM ET; OPRA's historical availability often
    lags hours behind real time, so a deferred/failed pull leaves the
    daily "flow" flag unset and the scheduler retries every 30 min until
    both symbols land (pull_opening_flow is idempotent per symbol/day).
    """
    if not HAS_OPT_FLOW:
        _daily_refresh_done["flow"] = True   # nothing to retry for
        return
    all_ok = True
    try:
        for sym in ("SPY", "QQQ"):
            try:
                flow = options_flow.pull_opening_flow(sym)
                if flow:
                    classification = options_flow.classify_flow(flow)
                    log("Opening flow {}: {} (imbalance {:+.2f})".format(
                        sym, classification["label"], classification["imbalance"]))
                else:
                    all_ok = False
            except Exception as e:
                log("Flow pull {} error: {}".format(sym, e))
                all_ok = False
    except Exception as e:
        log("Opening flow error: {}".format(e))
        all_ok = False
    if all_ok:
        _daily_refresh_done["flow"] = True


def _refresh_iv_snapshots():
    """
    Daily ATM IV snapshot for every symbol. Builds the rolling 1yr history
    needed for IV Rank / IV Percentile. Skips symbols where Alpaca options
    snapshot is unavailable.
    """
    if not HAS_IV_RANK:
        return
    try:
        all_syms = list(SYMBOLS)
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


def _run_iv_backfill_once():
    """One-shot seed of iv_history.db with ~252 days of ATM IV per symbol.

    compute_iv_rank needs 30+ days of history before it returns anything,
    and iv_history.db only began surviving redeploys once it moved onto the
    persistent volume -- so the rolling window starts empty. This seeds it
    once; the daily 4:15 PM snapshot maintains it from then on.

    Cost controls (the single-name path is a paid full-year OPRA pull):
      * persistent daily_marker 'iv_backfill' -- never re-runs once a full
        pass completes with Databento reachable;
      * symbols that already hold >=200 rows are skipped, so a partial run
        resumes instead of re-buying what it already wrote;
      * iv_backfill prices every pull via the free get_cost call and stops
        adding spend at its per-symbol/total caps. Budget-capped symbols
        leave the marker unset and are picked up by the next boot's run
        (each with a fresh budget), walking the universe forward in
        bounded-cost slices.
    """
    if not HAS_IV_RANK:
        return
    if _sweep_marker_get("iv_backfill"):
        return
    try:
        import iv_backfill
        import databento_adapter
    except ImportError as e:
        log("IV backfill unavailable: {}".format(e))
        return

    today_ymd = datetime.now(
        pytz.timezone("America/New_York")).date().isoformat()

    need = []
    for sym in sorted(SYMBOLS):
        # Structural no-data names (no OPRA history despite a paid pull)
        # carry a per-symbol marker so retries don't re-bill dead queries.
        if _sweep_marker_get("ivbf_nodata_" + sym):
            continue
        try:
            if len(iv_rank.get_history(sym, 252)) >= 200:
                continue
        except Exception:
            pass
        need.append(sym)
    if not need:
        _sweep_marker_set("iv_backfill", today_ymd)
        log("IV backfill: all symbols already seeded; marker set")
        return

    log("IV backfill: seeding {} symbols (caps: ${}/symbol, ${} total)".format(
        len(need), iv_backfill.MAX_PER_SYMBOL_USD, iv_backfill.MAX_TOTAL_USD))
    seeded = deferred = no_data = 0
    for sym in need:
        is_proxy = sym in iv_backfill.VIX_PROXY_MAP
        # The billing breaker can trip on any paid pull in this loop; once
        # open, every further Databento symbol is dead on arrival -- defer
        # them instead of classifying the silence as "no data".
        if not is_proxy and not databento_adapter.is_available():
            deferred += 1
            continue
        try:
            r = iv_backfill.backfill_symbol(sym)
        except Exception as e:
            log("IV backfill {} error: {}".format(sym, e))
            deferred += 1
            continue
        if r.get("rows", 0) > 0:
            seeded += 1
        elif r.get("skip_reason") in ("budget", "estimate_failed"):
            deferred += 1
        elif is_proxy or not databento_adapter.is_available():
            # Proxy seeds are free (yfinance) -- always worth retrying on a
            # later boot. A paid pull that "failed" with the breaker now
            # open was billed-blocked mid-flight, not empty: also retry.
            deferred += 1
        else:
            no_data += 1
            _sweep_marker_set("ivbf_nodata_" + sym, today_ymd)
    log("IV backfill: seeded={} deferred={} no_data={} of {}".format(
        seeded, deferred, no_data, len(need)))

    # Mark fully complete only when nothing is left to retry. Deferred
    # symbols (budget caps, breaker, yfinance-blocked proxies) leave the
    # marker unset so the next boot resumes -- already-seeded symbols are
    # excluded by the row-count check, so resuming never re-buys them.
    if deferred == 0:
        _sweep_marker_set("iv_backfill", today_ymd)


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
#   - High-IVR flags on the SPY/QQQ products (VRP read)
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
        regime         = _market_state.get("regime")
        premarket      = _market_state.get("premarket_brief")
        gex_bias       = _market_state.get("gex_bias")
        index_insights = _market_state.get("index_insights") or {}

    # High-IVR flags on the tradeable products. Each entry is
    # (symbol, iv_rank, iv_rv_ratio, vrp_favorable). VRP-favorable names
    # bubble to the top of the brief.
    high_ivr = []
    if HAS_IV_RANK:
        try:
            for sym in sorted(SYMBOLS):
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

    # Overnight context. The heading must say what units the numbers are in:
    # real futures points on the Databento/Yahoo paths, SPY dollars on the
    # ETF-proxy path (which only covers the 4:00-9:30 AM premarket and misses
    # the true overnight extremes — flag it so the range isn't trusted).
    if premarket:
        es = premarket.get("es_overnight") or {}
        if es:
            if es.get("source") == "futures":
                msg_lines.append("ES OVERNIGHT (futures pts):")
            else:
                msg_lines.append("ES OVERNIGHT (⚠ SPY proxy $ — premarket "
                                 "only, misses true ON range):")
            msg_lines.append("  Range: {:,.2f} – {:,.2f}".format(
                es.get("low", 0), es.get("high", 0)))
            msg_lines.append("  Close: {:,.2f} ({:.0%} of range)".format(
                es.get("last_print", 0), es.get("close_loc", 0)))
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

    # Real-index premarket context + options insights (actual SPX/NDX points)
    for idx, ctx_key in (("SPX", "spx"), ("NDX", "ndx")):
        ctx = (premarket or {}).get(ctx_key) or {}
        ins = index_insights.get(idx) or {}
        if not ctx and not ins:
            continue
        msg_lines.append("{} (index pts):".format(idx))
        if ctx:
            msg_lines.append("  Prev close: {:,.2f} | Implied open: {:,.2f} ({:+.2f}%)".format(
                ctx["prev_close"], ctx["implied_open"], ctx["gap_pct"]))
            msg_lines.append("  ON range: {:,.2f} – {:,.2f}".format(
                ctx["on_low"], ctx["on_high"]))
            if ctx.get("premarket_class"):
                msg_lines.append("  Open: {} — {}".format(
                    ctx["premarket_class"], ctx.get("premarket_note", "")))
        if ins:
            exp_tag = "0DTE" if ins.get("is_0dte") else \
                      "{}DTE".format(ins.get("dte"))
            if ins.get("expected_move_pts") is not None:
                msg_lines.append(
                    "  {} exp move: ±{:,.0f} pts (±{:.2f}%) → {:,.0f} / {:,.0f}".format(
                        exp_tag, ins["expected_move_pts"],
                        ins["expected_move_pct"],
                        ins["em_low"], ins["em_high"]))
            walls = []
            if ins.get("put_wall"):
                walls.append("Put wall {:,.0f}".format(ins["put_wall"]))
            if ins.get("zero_gamma"):
                walls.append("Zero-γ {:,.0f}".format(ins["zero_gamma"]))
            if ins.get("call_wall"):
                walls.append("Call wall {:,.0f}".format(ins["call_wall"]))
            if walls:
                msg_lines.append("  " + " | ".join(walls))
            extras = []
            if ins.get("pc_oi") is not None:
                extras.append("P/C OI {:.2f}".format(ins["pc_oi"]))
            if ins.get("gex_b") is not None:
                extras.append("GEX ${}B {}".format(
                    ins["gex_b"], ins.get("gex_regime", "")))
            if extras:
                msg_lines.append("  " + " | ".join(extras))
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
        enabled  = [k for k in ("orb","vwap_trend","vwap_mr","ib_extension")
                    if rules.get(k)]
        disabled = [k for k in ("orb","vwap_trend","vwap_mr","ib_extension")
                    if not rules.get(k)]
        msg_lines.append("STRATEGIES TODAY:")
        if enabled:
            msg_lines.append("  ✓ " + ", ".join(enabled))
        if disabled:
            msg_lines.append("  ✗ " + ", ".join(disabled))
        msg_lines.append("  Conviction: x{:.2f}".format(rules.get("conviction_multiplier", 1.0)))
        msg_lines.append("")

    # Tag high IVR with the VRP ratio so we can see at a glance whether IV
    # is actually rich vs realized -- a 70 IVR with a 1.0 ratio is a trap
    # (vol earned what it priced).
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


# Intraday de-risk thresholds. A genuine vol spike needs BOTH a strong jump
# over the trailing 20-day RV (so we don't react to a quiet tape) AND an
# absolute floor near the regime boundary (so a big multiple off a tiny base
# doesn't masquerade as a shock).
_DERISK_ELEVATED_RATIO = 1.5
_DERISK_ELEVATED_ABS   = 18.0
_DERISK_CRISIS_RATIO   = 2.0
_DERISK_CRISIS_ABS     = 26.0
_UNLOCK_RATIO          = 1.3


def _intraday_rv_spy():
    """Annualized realized vol (%) from the last ~60 min of SPY 5-min bars,
    or None when there isn't enough clean data."""
    intraday = get_intraday("SPY")
    if not intraday or len(intraday) < 12:
        return None
    closes = [b["c"] for b in intraday[-12:]]
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0:
            return None
        rets.append(math.log(closes[i] / closes[i - 1]))
    if len(rets) < 2:
        return None
    # 78 5-min bars/day * 252 trading days = 19656.
    return statistics.stdev(rets) * math.sqrt(19656) * 100


def _apply_intraday_regime(new_label, rv_intra, rv20, reason, blurb):
    """Swap the live regime label + strategy matrix in shared state and notify.
    Returns the prior label."""
    with _market_state_lock:
        regime_now = dict(_market_state.get("regime") or {})
        prior = regime_now.get("regime")
        regime_now["regime"] = new_label
        regime_now["rules"]  = dict(regime_filter.REGIME_STRATEGY_RULES[new_label])
        regime_now["note"]   = "{} RV_intra={:.1f}% (RV20={:.1f}%).".format(
            reason, rv_intra, rv20)
        regime_now["intraday_flip"] = True
        regime_now["rv_intra"]      = round(rv_intra, 2)
        _market_state["regime"] = regime_now
    log("REGIME FLIP: {} -> {} (rv_intra={:.1f}%)".format(
        prior, new_label, rv_intra))
    try:
        send_telegram("REGIME UPDATE: {} -> {}\n{}".format(prior, new_label, blurb))
    except Exception as e:
        log("regime flip telegram error: {}".format(e))
    return prior


def _intraday_regime_recheck():
    """
    Re-classify the live regime against intraday realized vol. Safe to call
    every scan from ~10:00 AM ET on (not just once at 10:30):

      * De-risk (escalate): a sharp intraday vol expansion that implies a more
        hostile regime than the live label force-flips UP to ELEVATED (mean
        reversion off, conviction trimmed) or CRISIS. Strictly monotonic -- we only escalate intraday, never relax, and
        never re-fire for a regime already reached today. This is the gap the
        old once-at-10:30 unlock left open: a NORMAL/LOW_VOL morning that turns
        violent used to keep trading mean reversion into the spike.
      * Unlock (coiled spring): the original COMPRESSED -> LOW_VOL flip, fired
        once, when a tight-range morning expands but not violently.
    """
    if not HAS_REGIME:
        return

    et    = pytz.timezone("America/New_York")
    today = datetime.now(et).strftime("%Y-%m-%d")
    if _regime_recheck_done.get("date") != today:
        _regime_recheck_done.clear()
        _regime_recheck_done.update(
            {"date": today, "unlocked": False, "escalated_rank": -1})

    rv_intra = _intraday_rv_spy()
    if rv_intra is None:
        return

    with _market_state_lock:
        regime_now = _market_state.get("regime") or {}
    current_label = regime_now.get("regime")
    rv20          = regime_now.get("realized") or 0
    cur_rank      = regime_filter.regime_rank(current_label)
    _regime_recheck_done["rv_intra"] = round(rv_intra, 2)

    # --- De-risk: escalate to a more hostile regime on a real vol spike ---
    target = None
    if (rv20 > 0 and rv_intra >= _DERISK_CRISIS_ABS
            and rv_intra > rv20 * _DERISK_CRISIS_RATIO):
        target = "CRISIS"
    elif (rv20 > 0 and rv_intra >= _DERISK_ELEVATED_ABS
            and rv_intra > rv20 * _DERISK_ELEVATED_RATIO):
        target = "ELEVATED"

    if target:
        target_rank = regime_filter.regime_rank(target)
        if (target_rank > cur_rank
                and target_rank > _regime_recheck_done.get("escalated_rank", -1)):
            _apply_intraday_regime(
                target, rv_intra, rv20,
                "INTRADAY_DERISK to {}.".format(target),
                ("Intraday vol spiking ({:.1f}% vs RV20 {:.1f}%).\n"
                 "Mean reversion disabled, conviction trimmed for the rest of "
                 "the session.").format(rv_intra, rv20))
            _regime_recheck_done["escalated_rank"] = target_rank
        return

    # --- Unlock: COMPRESSED coiled spring -> LOW_VOL (one-shot) ---
    if (current_label == "COMPRESSED"
            and not _regime_recheck_done.get("unlocked")
            and rv20 > 0 and rv_intra > rv20 * _UNLOCK_RATIO):
        _apply_intraday_regime(
            "LOW_VOL", rv_intra, rv20, "INTRADAY_FLIP from COMPRESSED.",
            ("Intraday RV expanding ({:.1f}% vs RV20 {:.1f}%).\n"
             "Trend strategies fully active for the rest of the session.")
            .format(rv_intra, rv20))
        _regime_recheck_done["unlocked"] = True


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


_daily_refresh_done = {"date": None, "vol": False}
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

# Per-day intraday-recheck state. `escalated_rank` is the severity index of the
# most hostile regime we've de-risked into today (-1 = none), keeping escalation
# monotonic; `unlocked` guards the one-shot COMPRESSED -> LOW_VOL flip.
_regime_recheck_done = {"date": None, "unlocked": False, "escalated_rank": -1}


def vol1d_updater_loop():
    """Dedicated fast loop (~vol1d.update_secs, default 15s) computing the
    VIX1D proxy state through RTH and publishing it into _market_state.

    This is the only fast-cadence component in the engine — the 5-min scan
    and the nightly GEX build stay untouched. The chain source is CBOE's
    free delayed-quotes CDN, so cadence costs bandwidth, not money. Outside
    RTH (or on holidays) the loop idles; the last state of the session
    stays readable until the next open.
    """
    if not HAS_VOL1D:
        return
    log("vol1d updater started (interval {}s, enforce={})".format(
        vol1d_config.get_config()["update_secs"],
        vol1d_config.get_config()["enforce"]))
    updater = None
    et = pytz.timezone("America/New_York")
    while True:
        interval = vol1d_config.get_config()["update_secs"]
        try:
            now = datetime.now(et)
            et_hour = now.hour + now.minute / 60.0
            if is_trading_day() and 9.5 <= et_hour < 16.05:
                if updater is None:
                    updater = vol1d_state.Vol1DUpdater()
                with _market_state_lock:
                    gex = _market_state.get("gex_bias")
                st = updater.compute_once(gex_bias=gex)
                if st is not None:
                    with _market_state_lock:
                        _market_state["vol1d"]    = st
                        _market_state["vol1d_ts"] = time.time()
                        # Intraday index GEX rides the same pass. Published
                        # separately — the nightly gex_bias and its
                        # consumers stay untouched.
                        _market_state["gex_live"] = updater.gex_live
            else:
                # Fresh accumulators next session; idle at a slow tick.
                updater = None
                interval = max(interval, 60)
        except Exception as e:
            log("vol1d updater error: {}".format(e))
        time.sleep(interval)


def get_vol1d_state():
    """Latest Vol1DState (or None). The single read-side accessor the rest
    of the engine uses."""
    with _market_state_lock:
        return _market_state.get("vol1d")


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
    if boot_hour >= 8.0:
        threading.Thread(target=_refresh_volume_profiles, daemon=True).start()
        # Build yesterday's Market Profile (needed for today's opening classification)
        threading.Thread(target=_build_market_profile, daemon=True).start()
    # Daily jobs only run on a trading session (skip weekends/holidays).
    _is_session = is_trading_day()
    # Brief on boot ONLY if we're in the pre-open window (9:00 AM – 10:30 AM ET)
    if _is_session and 9.0 <= boot_hour <= 10.5:
        threading.Thread(target=run_premarket_brief, daemon=True).start()
    # Opening flow: no boot-time spawn -- the scheduler's 10:05-16:00 retry
    # loop fires on its first tick (the pull is idempotent per symbol/day,
    # so a re-attempt after an earlier success costs one metadata call).
    if _is_session and boot_hour >= 16.25:
        threading.Thread(target=_refresh_iv_snapshots, daemon=True).start()
    # Paper replay on a late boot. The done-flag below is pre-marked for
    # post-16:05 boots, which used to mean a redeploy after the EOD replay
    # window silently lost the whole day's synthetic outcomes (no boot-time
    # spawn existed, unlike IV/GEX/OI). The replay dedupes on paper_key, so
    # re-running when the pre-redeploy process already covered today is free.
    if _is_session and HAS_PAPER_TRADER and boot_hour >= 16.05:
        def _paper_boot_run():
            try:
                paper_trader.run_paper_trader(DB_FILE, log_fn=log)
            except Exception as _e:
                log("paper trader (boot) error: {}".format(_e))
        threading.Thread(target=_paper_boot_run, daemon=True).start()
    # GEX/OI read OPRA's EOD statistics batch (published well after the close),
    # so they start at 5:30 PM ET. On a late boot, kick them once here too --
    # the persistent success marker and the retry throttle keep the recurring
    # scheduler from launching a duplicate run on its first iteration.
    if _is_session and boot_hour >= 17.5:
        if not _sweep_already_done_today("gex") and _sweep_retry_due("gex"):
            threading.Thread(target=_refresh_gex_snapshots, daemon=True).start()
        if not _sweep_already_done_today("oi") and _sweep_retry_due("oi"):
            threading.Thread(target=_refresh_oi_snapshots, daemon=True).start()

    # One-shot IV history seed (see _run_iv_backfill_once; the persistent
    # marker makes this a no-op once a full pass has completed). Runs on any
    # boot regardless of session/hour -- it reads historical data, and the
    # sooner it lands the sooner the IV-rank-gated strategies go live.
    threading.Thread(target=_run_iv_backfill_once, daemon=True).start()

    # Mark today's refreshes as done so we don't double-run
    _daily_refresh_done["date"]       = today_str
    _daily_refresh_done["vol"]        = boot_hour >= 8.0
    _daily_refresh_done["mprofile"]   = boot_hour >= 8.0
    # flow stays False on boot: _pull_opening_flow sets it on success and the
    # scheduler retries through the session (OPRA availability lags intraday).
    _daily_refresh_done["flow"]       = False
    _daily_refresh_done["premarket"]  = (boot_hour >= 8.5)
    # gex/oi are governed by their own persistent success marker + retry
    # throttle (see _sweep_retry_due), not this in-memory done-flag.
    _daily_refresh_done["iv"]         = boot_hour >= 16.25
    _daily_refresh_done["paper"]      = boot_hour >= 16.05
    # vol1d EOD (baseline rebuild + official reconcile) runs at 16:30; on a
    # later boot mark it done — there are no fresh ticks to rebuild from.
    _daily_refresh_done["vol1d_eod"]  = boot_hour >= 16.5
    # Only relevant on Fridays; pre-mark on non-Fridays so it never fires.
    _is_friday = datetime.now(et).weekday() == 4
    _daily_refresh_done["friday_digest"] = (not _is_friday) or (boot_hour >= 16.25)
    _premarket_done["date"]           = today_str if boot_hour >= 9.4 else None
    _overnight_done["date"]           = today_str if boot_hour >= 15.93 else None

    while True:
        try:
            run_signal_scan()

            # --- AI improvement triggers ---
            et      = pytz.timezone("America/New_York")
            now_et  = datetime.now(et)
            today   = now_et.strftime("%Y-%m-%d")
            et_hour = now_et.hour + now_et.minute / 60.0

            # --- Daily maintenance tasks ---
            # Pre-market (6 AM ET): reset the daily refresh flags
            if et_hour >= 6.0 and _daily_refresh_done.get("date") != today:
                _daily_refresh_done["date"] = today
                _daily_refresh_done["vol"]       = False
                _daily_refresh_done["iv"]        = False
                _daily_refresh_done["premarket"] = False
                _daily_refresh_done["mprofile"]  = False
                _daily_refresh_done["flow"]      = False
                _daily_refresh_done["paper"]     = False
                _daily_refresh_done["vol1d_eod"] = False
                # Pre-mark non-Fridays so the digest never fires on Mon-Thu.
                _daily_refresh_done["friday_digest"] = (
                    datetime.now(et).weekday() != 4
                )
            # Holiday/weekend-aware: gates the premarket brief and every
            # EOD job so they don't fire (or spend on Databento) when the
            # market is closed. Cached per-day, so this is one cheap call.
            _session = is_trading_day()

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

            # 10:05 AM ET: pull SPY+QQQ options flow for the 8:00-10:00 window.
            # OPRA historical availability can lag hours behind real time, so
            # the done-flag is only set on success (inside _pull_opening_flow)
            # and a deferred pull retries every 30 min until the close.
            if (_session and 10.08 <= et_hour < 16.0
                    and not _daily_refresh_done.get("flow")
                    and time.time() - _daily_refresh_done.get("flow_attempt", 0)
                    > 1800):
                _daily_refresh_done["flow_attempt"] = time.time()
                threading.Thread(target=_pull_opening_flow, daemon=True).start()

            # Re-classify regime against intraday RV every scan from ~10:00 AM
            # to just before the close. The recheck is monotonic + idempotent
            # internally: it escalates (de-risks) the moment the tape turns
            # violent and fires the COMPRESSED unlock once, so running it
            # repeatedly catches an afternoon vol spike the old 10:30 one-shot
            # would have missed.
            if _session and 10.0 <= et_hour < 15.92:
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

            # Post-close GEX + OI snapshots (feed tomorrow's brief / OI-delta
            # tracking). Both read OPRA's EOD `statistics` schema, whose
            # open-interest batch is not published until well after the close,
            # so the old 4:30 PM sweep returned empty chains (chain_len=0) and
            # the fire-and-forget done-flag then lost the data until the next
            # process restart. Start at 5:30 PM ET and retry on a throttle
            # through ~8 PM, gating on the persistent success marker so we stop
            # the moment a pull lands (and never re-run a sweep already paid
            # for). IV stays earlier -- it pulls live Alpaca snapshots, not the
            # Databento EOD batch, so it is available right after the close.
            if (_session and 17.5 <= et_hour < 20.0
                    and not _sweep_already_done_today("gex")
                    and _sweep_retry_due("gex")):
                threading.Thread(target=_refresh_gex_snapshots, daemon=True).start()

            if (_session and 17.58 <= et_hour < 20.0
                    and not _sweep_already_done_today("oi")
                    and _sweep_retry_due("oi")):
                threading.Thread(target=_refresh_oi_snapshots, daemon=True).start()

            # 4:15 PM ET: daily IV snapshot (both modes — builds rolling history)
            # 4:30 PM ET: vol1d EOD — reconcile today's proxy close vs the
            # official VIX1D print and rebuild the minute-of-day baseline
            # with today's ticks included.
            if (_session and HAS_VOL1D and et_hour >= 16.5
                    and not _daily_refresh_done.get("vol1d_eod")):
                _daily_refresh_done["vol1d_eod"] = True
                def _vol1d_eod():
                    try:
                        summary = vol1d_state.run_nightly_jobs()
                        log("vol1d EOD: {}".format(summary))
                    except Exception as _e:
                        log("vol1d EOD error: {}".format(_e))
                threading.Thread(target=_vol1d_eod, daemon=True).start()

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

<!-- SIGNAL CARDS -->
<div style="padding:12px 14px 0">
  <div style="font-size:10px;font-weight:700;color:#8b949e;
              text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px">
    INTRADAY (SPY/QQQ 0DTE) &mdash; {nsig} setup{pl_s} found
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
              AND COALESCE(mode, '') != 'paper_t2'
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


@app.route("/scan/unified")
def scan_unified():
    """JSON view of the tiered signal payload (intraday + context)."""
    with state_lock:
        intraday = [r for r in all_signals
                    if r.get("status") in ("SIGNAL", "SIGNAL (no options)")]
    context = {"index": index_context()}
    if HAS_SCANNER_CORE:
        return jsonify(scanner_core.merge_and_rank(intraday, [], context))
    return jsonify({"intraday": intraday, "context": context})


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

0DTE WATCHING ({nwatch} symbols):
{dte_watch}

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
I have full access to your live 0DTE signals, open trades, P&L, and scanner config. Ask me anything:

<div style='margin-top:10px'>
<span class='chip' onclick='ask(this)'>What's the best setup right now?</span>
<span class='chip' onclick='ask(this)'>Explain the top 0DTE signal</span>
<span class='chip' onclick='ask(this)'>Should I be bullish or bearish today?</span>
<span class='chip' onclick='ask(this)'>What are my open positions?</span>
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

        system_prompt = """You are an expert trading assistant embedded inside a live 0DTE options scanner focused on SPX/NDX exposure via SPY/QQQ. You have direct access to the scanner's current state shown below.

You help the trader:
- Interpret and rank the current signals
- Explain what the scanner is seeing and why
- Give conviction ratings on specific trades
- Flag risks (counter-trend, low volume, extended moves)
- Answer general questions about options, technical analysis, and market structure

Be concise and direct -- this is a trading app on mobile. Use short paragraphs. Lead with the most actionable insight. Use dollar signs and percentages as shown in the data.

When discussing options: always mention the DTE and the delta.

--- LIVE SCANNER DATA ---
{ctx}
--- END SCANNER DATA ---

You have full knowledge of this scanner's methodology:
- 0DTE signals use ORB breakouts, VWAP, confluence grading (A/B/C), volume, gap alignment, RS vs SPY, 1hr trend
- VWAP Trend signals fire when price stays consistently on one side of VWAP with trending structure (based on Zarattini & Aziz SSRN 2023 research)
- VWAP Mean Reversion signals fire when price touches VWAP deviation bands and reverses back toward the mean (best on range-bound days)
- IB Extension signals fire after 10:30 AM when price moves beyond the Initial Balance range (Market Profile methodology)
- VWAP Reclaim signals detect when price reclaims VWAP after being on the wrong side, with volume confirmation
- 0DTE options target delta ~0.40""".format(ctx=ctx)

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
            "storage": _storage_status(),
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

@app.route("/vol1d")
def vol1d_endpoint():
    """Latest VIX1D proxy state (shadow-mode diagnostics)."""
    if not HAS_VOL1D:
        return jsonify({"status": "unavailable", "module_loaded": False})
    st = get_vol1d_state()
    with _market_state_lock:
        ts = _market_state.get("vol1d_ts", 0)
        gex_live = _market_state.get("gex_live")
    try:
        from vol1d import baseline as _bl
        from vol1d import qa as _qa
        banked = _bl.sessions_banked()
        resid_date, resid = _qa.latest_residual()
    except Exception:
        banked, resid_date, resid = None, None, None
    return jsonify({
        "status":           "ok" if st else "no_data",
        "state":            st.to_dict() if st else None,
        "age_secs":         round(time.time() - ts, 1) if ts else None,
        "enforce":          vol1d_config.get_config()["enforce"],
        "baseline_sessions": banked,
        "last_residual":    {"date": resid_date, "value": resid},
        "gex_live":         gex_live,
    })


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
    """Premarket brief: overnight futures range, inventory, gap class,
    real-index (SPX/NDX) context and options insights."""
    with _market_state_lock:
        brief    = _market_state.get("premarket_brief")
        insights = _market_state.get("index_insights")
    if not brief:
        return jsonify({
            "status":        "no_data",
            "note":          "Premarket brief refreshes at 9:10 AM ET.",
            "module_loaded": HAS_OVERNIGHT,
        })
    return jsonify({
        "status":         "ok",
        "brief":          brief,
        "index_insights": insights,
    })


@app.route("/iv")
def iv_endpoint():
    """IV Rank for all tracked symbols."""
    if not HAS_IV_RANK:
        return jsonify({"status": "no_module", "note": "iv_rank module not loaded"})
    out = []
    for sym in sorted(SYMBOLS):
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
        regime         = _market_state.get("regime")
        brief          = _market_state.get("premarket_brief")
        gex_bias       = _market_state.get("gex_bias")
        index_insights = _market_state.get("index_insights")

    # Top 5 high-IVR names
    top_iv = []
    if HAS_IV_RANK:
        try:
            for sym in sorted(SYMBOLS):
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
        "index_insights":     index_insights,
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
        db_test["billing"]       = databento_adapter.billing_status()

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

    # Feed-mismatch health: how many symbols are falling back to the biased
    # volume estimator today, and whether an auto-rebuild has fired.
    out["volume_health"] = volume_health()

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
init_vol1d_journal_columns()
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
_log_storage_status()   # loud WARN/ERROR if storage is ephemeral or unwritable
db_load_latest_config()   # restore AI config from last session
_maybe_reset_ai_baseline()  # one-time wipe of pre-reset drifted tuning
threading.Thread(target=background_scheduler, daemon=True).start()
threading.Thread(target=telegram_poller,      daemon=True).start()
if HAS_VOL1D:
    threading.Thread(target=vol1d_updater_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
