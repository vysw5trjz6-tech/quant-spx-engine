# regime_filter.py
# Volatility regime detection — scales position size and toggles strategies.
#
# Why: ORB win rate collapses in compressed-range regimes (low VIX + tight ATR).
# VWAP mean reversion works WORSE in crisis regimes. Trend strategies work BEST
# in expanding-range regimes. Treating all days the same is leaving alpha on
# the table and accepting losses you don't have to take.

import os
import io
import csv
import math
import statistics
import requests
from datetime import datetime, timedelta, timezone
import pytz

from bar_utils import sanitize_bars

try:
    import db_utils
    _HAS_DB = True
except Exception:
    _HAS_DB = False

ALPACA_KEY    = os.getenv("APCA_API_KEY_ID", "").strip()
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY", "").strip()
HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}
DATA_URL = "https://data.alpaca.markets/v2/stocks/{}/bars"


# =============================================
# REGIME CLASSIFICATION
# =============================================
#
# Five regimes, classified by VIX level + 20-day realized vol percentile:
#
#   COMPRESSED  : VIX < 13          → ORB ranges too tight, expect chop
#   LOW_VOL     : VIX 13-16         → trend follows, mean reversion works
#   NORMAL      : VIX 16-22         → all strategies fine
#   ELEVATED    : VIX 22-30         → momentum strong, mean reversion dangerous
#   CRISIS      : VIX > 30          → only intraday, no swing, no mean reversion

REGIME_THRESHOLDS = [
    (13, "COMPRESSED"),
    (16, "LOW_VOL"),
    (22, "NORMAL"),
    (30, "ELEVATED"),
    (999, "CRISIS"),
]

# Strategy enable/disable matrix per regime.
#
# IMPORTANT: COMPRESSED no longer hard-disables trend strategies. Trailing-RV20
# below 10% is a coiled-spring signature -- historically it predicts expansion
# days more often than chop days. Disabling ORB/vwap_trend means we miss the
# moves we're best positioned to catch. Instead we keep them enabled at 0.5x
# size with a score penalty so only the strongest setups fire.
#
# conviction_multiplier: a regime confidence weight (NOT position size, which
# is no longer modeled). It scales the signal score during ranking/grading so
# that signals fired in a favorable regime rank above identical signals fired
# in a hostile one. Compounds with the GEX conviction_mult.
REGIME_STRATEGY_RULES = {
    "COMPRESSED": {
        "orb":              True,
        "vwap_trend":       True,
        "vwap_mr":          True,    # range-bound days favor MR
        "ib_extension":     True,
        "swing_breakout":   False,
        "conviction_multiplier":  0.5,     # low conviction — premium bleeds in low IV
        # Raise the bar for trend signals to fire while in COMPRESSED.
        # Scanner subtracts this from grade_pts before letter assignment.
        "score_penalty_trend": 15,
    },
    "LOW_VOL": {
        "orb":              True,
        "vwap_trend":       True,
        "vwap_mr":          True,
        "ib_extension":     True,
        "swing_breakout":   True,
        "conviction_multiplier":  0.85,
    },
    "NORMAL": {
        "orb":              True,
        "vwap_trend":       True,
        "vwap_mr":          True,
        "ib_extension":     True,
        "swing_breakout":   True,
        "conviction_multiplier":  1.0,
    },
    "ELEVATED": {
        "orb":              True,
        "vwap_trend":       True,
        "vwap_mr":          False,   # bands break in high vol
        "ib_extension":     True,
        "swing_breakout":   True,
        "conviction_multiplier":  0.85,    # higher option prices, lower conviction
    },
    "CRISIS": {
        "orb":              True,
        "vwap_trend":       True,
        "vwap_mr":          False,
        "ib_extension":     True,
        "swing_breakout":   False,   # gaps gap; swings get blown up
        "conviction_multiplier":  0.5,     # lowest conviction, wider stops
    },
}


# =============================================
# DATA FETCH
# =============================================

# =============================================
# VIX SOURCING (multi-source, cached)
# =============================================
#
# The whole regime classifier hinges on VIX / VIX9D / VIX3M. Yahoo via
# yfinance is the cheapest source but is unreliable from datacenter IPs
# (Railway): it rate-limits and returns empty intermittently. To stop the
# regime from going blank on a transient hiccup we layer:
#
#   1. Yahoo (yfinance)            -- primary, freshest
#   2. Stooq free daily CSV        -- key-less, datacenter-friendly
#   3. Persistent last-good cache  -- survives a full outage
#
# Cached values are accepted up to _VIX_CACHE_MAX_AGE old. Regime buckets
# are coarse and this feeds a pre-market alert, so a day-old VIX is far
# better than no regime at all (the original docstring already says
# day-old is acceptable here).

_VIX_CACHE_DB      = ((os.getenv("DATA_DIR")
                       or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
                       or "/tmp").rstrip("/") + "/regime_cache.db")
# 4 days covers a Fri close consumed after a 3-day holiday weekend.
_VIX_CACHE_MAX_AGE = timedelta(days=4)

_STOOQ_SYMBOLS = {
    "^VIX":   "%5Evix",
    "^VIX9D": "%5Evix9d",
    "^VIX3M": "%5Evix3m",
}


def _vix_cache_init(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS vix_last_good "
                 "(ticker TEXT PRIMARY KEY, value REAL, stored_at TEXT)")


def _vix_cache_set(ticker, value):
    if not _HAS_DB:
        return
    try:
        conn = db_utils.connect(_VIX_CACHE_DB)
        _vix_cache_init(conn)
        conn.execute("INSERT OR REPLACE INTO vix_last_good "
                     "(ticker, value, stored_at) VALUES (?, ?, ?)",
                     (ticker, float(value),
                      datetime.now(timezone.utc).isoformat()))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _vix_cache_get(ticker):
    """Return last-good value if present and within max age, else None."""
    if not _HAS_DB:
        return None
    try:
        conn = db_utils.connect(_VIX_CACHE_DB)
        _vix_cache_init(conn)
        row = conn.execute("SELECT value, stored_at FROM vix_last_good "
                           "WHERE ticker = ?", (ticker,)).fetchone()
        conn.close()
        if not row:
            return None
        stored = datetime.fromisoformat(row[1])
        if datetime.now(timezone.utc) - stored > _VIX_CACHE_MAX_AGE:
            print("[regime] cached {} is stale; ignoring".format(ticker))
            return None
        return float(row[0])
    except Exception:
        return None


def _fetch_stooq_close(ticker):
    """Latest daily close for a ^-index from Stooq's free CSV. None on fail."""
    sym = _STOOQ_SYMBOLS.get(ticker)
    if not sym:
        return None
    try:
        r = requests.get("https://stooq.com/q/d/l/?s={}&i=d".format(sym),
                         timeout=10)
        if r.status_code != 200 or not r.text:
            return None
        rows = list(csv.DictReader(io.StringIO(r.text)))
        if not rows or "Close" not in rows[-1]:
            return None
        return float(rows[-1]["Close"])
    except Exception as e:
        print("[regime] Stooq {} fetch failed: {}".format(ticker, e))
        return None


def _fetch_yahoo_close(ticker):
    """Latest daily close via yfinance. None on any failure."""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="5d", interval="1d")
        if hist is not None and not hist.empty:
            return float(hist["Close"].iloc[-1])
    except ImportError:
        print("[regime] yfinance not installed")
    except Exception as e:
        print("[regime] Yahoo {} fetch failed: {}".format(ticker, e))
    return None


def _fetch_yahoo_download(ticker):
    """
    Alternate Yahoo path -- uses yf.download() (chart API) instead of
    Ticker.history() (quote API). They hit different upstream endpoints
    and Yahoo's blocking is inconsistent across them, so trying both
    occasionally rescues a datacenter-blocked deployment.
    """
    try:
        import yfinance as yf
        df = yf.download(ticker, period="5d", interval="1d",
                         progress=False, auto_adjust=False, threads=False)
        if df is None or df.empty:
            return None
        close = df["Close"].iloc[-1]
        # yf.download can return a MultiIndex column even for one ticker.
        if hasattr(close, "iloc"):
            close = close.iloc[0]
        return float(close)
    except ImportError:
        return None
    except Exception as e:
        print("[regime] Yahoo download() {} failed: {}".format(ticker, e))
        return None


def _fetch_databento_vix(ticker):
    """
    Spot-VIX proxy via the front-month VX future on Databento. Only valid
    for the spot VIX itself (^VIX) -- VIX9D/VIX3M are separate indices the
    VX front-month does not represent, so we return None for those and let
    them fall through to the other sources.

    Databento is reachable from datacenter IPs where Yahoo/Stooq are not,
    so this is the primary source for the spot regime read.
    """
    if ticker != "^VIX":
        return None
    try:
        import databento_adapter
        if not databento_adapter.is_available():
            return None
        return databento_adapter.get_vix_proxy()
    except Exception as e:
        print("[regime] Databento VX proxy failed: {}".format(e))
        return None


def _get_vix_value(ticker, lo=1.0, hi=200.0):
    """
    Source priority:
      1. Yahoo (history endpoint)            -- real spot if reachable
      2. Yahoo (download/chart endpoint)     -- alternate Yahoo route
      3. Databento VX front-month + slope    -- proxy, always reachable
      4. Stooq                               -- usually blocked on Railway
      5. Last-good cache                     -- final resort

    Real spot first, proxy second. If Yahoo is unblocked on this region
    we get the actual VIX index value; otherwise we fall through to the
    term-structure-adjusted VX estimate, which trails spot by ~1 pt.
    """
    for fetch in (_fetch_yahoo_close, _fetch_yahoo_download,
                  _fetch_databento_vix, _fetch_stooq_close):
        val = fetch(ticker)
        if val is not None and lo <= val <= hi:
            _vix_cache_set(ticker, val)
            return val
    cached = _vix_cache_get(ticker)
    if cached is not None:
        print("[regime] using cached last-good {}={:.2f}".format(
            ticker, cached))
    return cached


def get_current_vix():
    """
    Fetch the latest VIX quote.

    Source priority:
      1. Databento front-month VX future (reachable from datacenter IPs)
      2. Yahoo Finance / Stooq (real VIX index, free; often blocked on cloud)
      3. Persistent last-good cache (<= 4 days old)
      4. VIXY ETF as last-resort proxy with tight sanity guard
      5. None (caller uses realized vol fallback)

    The VX front-month is a proxy (small contango premium vs spot), but it
    is the only VIX source consistently reachable from Railway -- Yahoo and
    Stooq both fail there. Good enough for regime bucketing.
    """
    # --- Paths 1-2: real VIX (Yahoo -> Stooq -> last-good cache) ---
    vix = _get_vix_value("^VIX", 5, 80)
    if vix is not None:
        return vix

    # --- Path 3: VIXY ETF as proxy (last resort, tightly bounded) ---
    try:
        et    = pytz.timezone("America/New_York")
        end   = datetime.now(et)
        start = end - timedelta(days=5)
        params = {
            "timeframe": "1Day",
            "start":     start.strftime("%Y-%m-%d"),
            "end":       end.strftime("%Y-%m-%d"),
            "limit":     5,
            "feed":      "iex",
        }
        r = requests.get(DATA_URL.format("VIXY"), headers=HEADERS,
                         params=params, timeout=10)
        if r.status_code != 200:
            return None
        bars = r.json().get("bars", [])
        if not bars:
            return None
        vixy_close = bars[-1]["c"]
        est_vix    = vixy_close * 1.85

        # Tight guard: VIXY × 1.85 only valid roughly 12-25 range
        if est_vix > 35 or est_vix < 5:
            print("[regime] VIXY proxy rejected: VIX={:.1f}".format(est_vix))
            return None
        return est_vix
    except Exception:
        return None


# =============================================
# RV20 PERCENTILE (for compression-squeeze detection)
# =============================================
#
# Returns the percentile rank of today's 20-day realized vol against its own
# rolling distribution over the past ~year. Below the 20th percentile means
# realized vol is in the bottom fifth of recent history -- a coiled-spring
# setup. Combined with a tight overnight gap and non-backwardated VIX term
# structure, this is the classic expansion-day signature.

def _rolling_rv20(closes):
    """Yield rolling 20-day annualized stdev (% form) over a daily close series."""
    if len(closes) < 21:
        return []
    out = []
    for end in range(21, len(closes) + 1):
        window = closes[end - 21:end]
        rets = []
        for i in range(1, len(window)):
            if window[i - 1] <= 0:
                rets = []
                break
            rets.append(math.log(window[i] / window[i - 1]))
        if len(rets) < 2:
            continue
        sd = statistics.stdev(rets)
        out.append(sd * math.sqrt(252) * 100)
    return out


def _equity_daily_bars(symbol, lookback_days, min_bars=30):
    """Daily bars for `symbol`, Databento-first (clean consolidated US-equities
    feed), Alpaca/IEX fallback. Sanitized either way so a single bad print
    can't poison the realized-vol or RV20 reads that set the trading regime --
    a corrupt close manufactures two large opposite log-returns and can shove
    the regime from NORMAL to CRISIS (or vice-versa). Returns an ascending list
    of {o,h,l,c,v,t}, or [] on failure."""
    # Primary: Databento consolidated equities.
    try:
        import databento_adapter
        if databento_adapter.is_available():
            now   = datetime.now(timezone.utc)
            start = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
            end   = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            bars  = sanitize_bars(
                symbol,
                databento_adapter.get_equity_bars(symbol, start, end, "ohlcv-1d"),
                "regime")
            if bars and len(bars) >= min_bars:
                return bars
    except Exception:
        pass

    # Fallback: Alpaca/IEX. sort=desc + reverse keeps the freshest bars when
    # the [start, now] window holds more than `limit` (Alpaca defaults to
    # ascending and would otherwise drop the most recent ones).
    try:
        et    = pytz.timezone("America/New_York")
        start = (datetime.now(et) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        params = {
            "timeframe": "1Day",
            "start":     start,
            "limit":     max(lookback_days, min_bars + 10),
            "feed":      "iex",
            "sort":      "desc",
        }
        r = requests.get(DATA_URL.format(symbol), headers=HEADERS,
                         params=params, timeout=10)
        if r.status_code != 200:
            return []
        bars = r.json().get("bars", []) or []
        bars.reverse()
        return sanitize_bars(symbol, bars, "regime")
    except Exception:
        return []


def get_rv20_percentile(symbol="SPY", history_days=252):
    """
    Returns (today_rv20, percentile_0_to_100) or (None, None) on failure.

    Percentile uses the rolling distribution: percentile 15 means today's
    RV20 is lower than 85% of the past year's RV20 readings.
    """
    bars = _equity_daily_bars(symbol, history_days + 40, min_bars=60)
    if len(bars) < 60:
        return None, None
    closes = [b["c"] for b in bars]

    series = _rolling_rv20(closes)
    if len(series) < 30:
        return None, None

    today = series[-1]
    # Inclusive lower-rank percentile
    below = sum(1 for v in series if v < today)
    pct   = round(below / len(series) * 100, 1)
    return round(today, 2), pct


# =============================================
# EXPANSION OVERRIDE
# =============================================
#
# Coiled-spring conditions: low trailing realized vol + tight overnight gap +
# non-backwardated term structure. Empirically these days break out far more
# often than they chop. When detected we override the COMPRESSED rules with
# LOW_VOL rules (trend strategies fully enabled, size back to 0.85x).
#
# This is what would have saved us from missing the SPY trend day on 2026-05-13:
# RV20 was 9.8% (low decile), gap was small, VIX term structure flat.

EXPANSION_RV_PERCENTILE_MAX = 20.0   # RV20 must sit in bottom 20% of trailing year
EXPANSION_GAP_PCT_MAX       = 0.50   # overnight gap must be < 0.5% (no pre-decided move)
EXPANSION_TERM_LABELS_OK    = {"CONTANGO", "DEEP_CONTANGO", "FLAT"}


def check_expansion_watch(rv_percentile, gap_pct_abs, term_structure_label):
    """Return True if all three coiled-spring conditions are satisfied."""
    if rv_percentile is None or gap_pct_abs is None:
        return False
    if rv_percentile > EXPANSION_RV_PERCENTILE_MAX:
        return False
    if gap_pct_abs > EXPANSION_GAP_PCT_MAX:
        return False
    if term_structure_label not in EXPANSION_TERM_LABELS_OK:
        return False
    return True


def apply_expansion_override(regime_data, gap_pct_abs=None, symbol="SPY"):
    """
    Mutates and returns regime_data. If today qualifies as EXPANSION_WATCH:
      - Replace strategy rules with LOW_VOL rules (trend strats fully on)
      - Drop the COMPRESSED score penalty
      - Bump conviction_multiplier from 0.5 -> 0.85
      - Attach expansion_watch=True + diagnostics for the brief
    """
    regime_data.setdefault("expansion_watch", False)

    if regime_data.get("regime") != "COMPRESSED":
        return regime_data

    rv_today, rv_pct = get_rv20_percentile(symbol)
    term             = get_vix_term_structure() or {}
    term_label       = term.get("label")

    qualifies = check_expansion_watch(rv_pct, gap_pct_abs, term_label)

    regime_data["rv20_percentile"] = rv_pct
    regime_data["term_structure"]  = term

    if qualifies:
        regime_data["expansion_watch"] = True
        regime_data["rules"]           = dict(REGIME_STRATEGY_RULES["LOW_VOL"])
        regime_data["note"] = (
            "EXPANSION_WATCH active. RV20 pct={} gap={}% term={}. "
            "Trend strategies unlocked at LOW_VOL sizing."
        ).format(rv_pct, gap_pct_abs, term_label)

    return regime_data


def get_realized_vol(symbol="SPY", lookback_days=20):
    """
    Annualized realized volatility from daily closes.
    Used to confirm/adjust the VIX-based regime — when implied >> realized,
    we're paying for vol we won't see.
    """
    try:
        bars = _equity_daily_bars(symbol, lookback_days + 15, min_bars=lookback_days)
        if len(bars) < lookback_days:
            return None
        closes = [b["c"] for b in bars[-lookback_days-1:]]
        rets = []
        for i in range(1, len(closes)):
            rets.append(math.log(closes[i] / closes[i-1]))
        if len(rets) < 2:
            return None
        sd = statistics.stdev(rets)
        return round(sd * math.sqrt(252) * 100, 2)  # annualized %
    except Exception:
        return None


# =============================================
# CLASSIFY
# =============================================

def classify_regime(vix=None, realized_vol=None):
    """
    Returns dict with regime classification and strategy rules.
    Auto-fetches VIX and realized vol if not provided.
    """
    if vix is None:
        vix = get_current_vix()
    if realized_vol is None:
        realized_vol = get_realized_vol("SPY")

    if vix is None:
        # Fallback: use realized as proxy (poor but better than nothing)
        if realized_vol is None:
            return {
                "regime":   "NORMAL",
                "vix":      None,
                "realized": None,
                "rules":    REGIME_STRATEGY_RULES["NORMAL"],
                "note":     "no_vol_data_default_to_normal",
            }
        # Realized > 25% annualized = elevated regime
        if   realized_vol < 10: regime = "COMPRESSED"
        elif realized_vol < 14: regime = "LOW_VOL"
        elif realized_vol < 20: regime = "NORMAL"
        elif realized_vol < 28: regime = "ELEVATED"
        else:                   regime = "CRISIS"
    else:
        regime = "NORMAL"
        for thresh, name in REGIME_THRESHOLDS:
            if vix < thresh:
                regime = name
                break

    rules = REGIME_STRATEGY_RULES[regime]

    # IV vs RV gap signal — if implied >> realized, premium is rich
    iv_rv_gap = None
    if vix is not None and realized_vol is not None:
        iv_rv_gap = round(vix - realized_vol, 1)

    return {
        "regime":     regime,
        "vix":        round(vix, 2) if vix else None,
        "realized":   realized_vol,
        "iv_rv_gap":  iv_rv_gap,
        "rules":      rules,
        "note":       _regime_note(regime, iv_rv_gap),
    }


def _regime_note(regime, iv_rv_gap):
    notes = {
        "COMPRESSED": "Tight ranges. ORB unreliable. Half size on any trade.",
        "LOW_VOL":    "Trend-friendly, IV cheap. Good for buying premium.",
        "NORMAL":     "All strategies active. Standard sizing.",
        "ELEVATED":   "Momentum strong. Avoid mean reversion. Premium expensive.",
        "CRISIS":     "Intraday only. No swings. Half size, wider stops.",
    }
    note = notes.get(regime, "")
    if iv_rv_gap is not None and iv_rv_gap > 8:
        note += " Premium VERY rich (IV-RV gap = {}).".format(iv_rv_gap)
    elif iv_rv_gap is not None and iv_rv_gap < -3:
        note += " Premium underpriced — RV exceeding IV."
    return note


# =============================================
# VIX TERM STRUCTURE
# =============================================
#
# VIX9D < VIX < VIX3M = normal contango (forward vol priced higher).
# VIX9D > VIX > VIX3M = backwardation (front-month panic). Historically
# one of the cleanest short-term mean-revert signals on SPX -- inversions
# unwind within 3-5 sessions ~75% of the time. Cheap to compute: 3 EOD
# index reads via yfinance.

def get_vix_term_structure():
    """
    Returns dict with VIX9D / VIX / VIX3M and a regime label, or None on failure.

      label one of:
        "DEEP_BACKWARDATION"  -- VIX9D / VIX3M > 1.10  (panic, fade reflex moves)
        "BACKWARDATION"       -- VIX9D > VIX3M         (cautious, expect bounce)
        "FLAT"                -- |VIX9D - VIX3M| < 5%  (mixed, no edge)
        "CONTANGO"            -- VIX3M > VIX9D         (normal, trend-friendly)
        "DEEP_CONTANGO"       -- VIX3M / VIX9D > 1.15  (complacent, vol-of-vol up)
    """
    # Each leg: Yahoo -> Stooq -> last-good cache. Only bail if a leg is
    # unavailable from every source (term structure needs all three).
    out = {}
    for tk in ("^VIX9D", "^VIX", "^VIX3M"):
        val = _get_vix_value(tk, 5, 90)
        if val is None:
            print("[regime] term-structure unavailable: no {}".format(tk))
            return None
        out[tk] = val

    vix9d, vix, vix3m = out["^VIX9D"], out["^VIX"], out["^VIX3M"]
    if vix9d <= 0 or vix3m <= 0:
        return None

    ratio = vix9d / vix3m  # >1 = backwardation, <1 = contango

    if ratio > 1.10:
        label = "DEEP_BACKWARDATION"
        bias  = "FADE_DOWNSIDE"
        note  = "Front-month panic. Historically reverts within 3-5 sessions."
    elif ratio > 1.02:
        label = "BACKWARDATION"
        bias  = "CAUTIOUS_LONG"
        note  = "Short-term stress. Bounce probable but not immediate."
    elif ratio > 0.97:
        label = "FLAT"
        bias  = "NEUTRAL"
        note  = "Term structure flat. No directional signal."
    elif ratio > 0.87:
        label = "CONTANGO"
        bias  = "TREND_FRIENDLY"
        note  = "Normal contango. Standard regime."
    else:
        label = "DEEP_CONTANGO"
        bias  = "WATCH_VOL_SPIKE"
        note  = "Very steep contango. Complacency risk -- watch for vol expansion."

    return {
        "vix9d":  round(vix9d, 2),
        "vix":    round(vix, 2),
        "vix3m":  round(vix3m, 2),
        "ratio":  round(ratio, 3),
        "label":  label,
        "bias":   bias,
        "note":   note,
    }


# =============================================
# INTEGRATION HELPERS
# =============================================

def is_strategy_allowed(strategy_name, regime_data=None):
    """
    Quick check used inside detect_*() functions.

    Usage in main.py:
        if not is_strategy_allowed("vwap_mr"):
            return None
    """
    if regime_data is None:
        regime_data = classify_regime()
    return regime_data["rules"].get(strategy_name, True)
