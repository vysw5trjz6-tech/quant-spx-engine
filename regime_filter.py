# regime_filter.py
# Volatility regime detection — scales position size and toggles strategies.
#
# Why: ORB win rate collapses in compressed-range regimes (low VIX + tight ATR).
# VWAP mean reversion works WORSE in crisis regimes. Trend strategies work BEST
# in expanding-range regimes. Treating all days the same is leaving alpha on
# the table and accepting losses you don't have to take.

import os
import math
import statistics
import requests
from datetime import datetime, timedelta
import pytz

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
REGIME_STRATEGY_RULES = {
    "COMPRESSED": {
        "orb":              True,
        "vwap_trend":       True,
        "vwap_mr":          True,    # range-bound days favor MR
        "ib_extension":     True,
        "swing_breakout":   False,
        "size_multiplier":  0.5,     # half size — premium bleeds in low IV
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
        "size_multiplier":  0.85,
    },
    "NORMAL": {
        "orb":              True,
        "vwap_trend":       True,
        "vwap_mr":          True,
        "ib_extension":     True,
        "swing_breakout":   True,
        "size_multiplier":  1.0,
    },
    "ELEVATED": {
        "orb":              True,
        "vwap_trend":       True,
        "vwap_mr":          False,   # bands break in high vol
        "ib_extension":     True,
        "swing_breakout":   True,
        "size_multiplier":  0.85,    # higher option prices, hold less
    },
    "CRISIS": {
        "orb":              True,
        "vwap_trend":       True,
        "vwap_mr":          False,
        "ib_extension":     True,
        "swing_breakout":   False,   # gaps gap; swings get blown up
        "size_multiplier":  0.5,     # half size, wider stops
    },
}


# =============================================
# DATA FETCH
# =============================================

def get_current_vix():
    """
    Fetch the latest VIX quote.

    Source priority:
      1. Yahoo Finance (real VIX index, free, 15-min delayed)
      2. VIXY ETF as last-resort proxy with tight sanity guard
      3. None (caller uses realized vol fallback)

    NOTE: Databento does NOT sell the VIX spot index — they only sell VIX
    futures (VX) on XCBF.PITCH which requires a paid live license. So
    Databento is intentionally NOT in the VIX path here.

    Yahoo's ^VIX is the actual CBOE VIX index value, 15-min delayed during
    RTH and EOD-current outside RTH. Fine for our pre-market regime alert.
    """
    # --- Path 1: Yahoo Finance ---
    try:
        import yfinance as yf
        ticker = yf.Ticker("^VIX")
        # 5 days gives us yesterday's close even on weekends/holidays
        hist = ticker.history(period="5d", interval="1d")
        if hist is not None and not hist.empty:
            vix = float(hist["Close"].iloc[-1])
            if 5 <= vix <= 80:
                return vix
            print("[regime] Yahoo VIX out of range: {}".format(vix))
    except ImportError:
        print("[regime] yfinance not installed — falling back to VIXY proxy")
    except Exception as e:
        print("[regime] Yahoo VIX fetch failed: {}".format(e))

    # --- Path 2: VIXY ETF as proxy (last resort, tightly bounded) ---
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


def get_rv20_percentile(symbol="SPY", history_days=252):
    """
    Returns (today_rv20, percentile_0_to_100) or (None, None) on failure.

    Percentile uses the rolling distribution: percentile 15 means today's
    RV20 is lower than 85% of the past year's RV20 readings.
    """
    try:
        et    = pytz.timezone("America/New_York")
        end   = datetime.now(et)
        start = end - timedelta(days=history_days + 40)
        params = {
            "timeframe": "1Day",
            "start":     start.strftime("%Y-%m-%d"),
            "end":       end.strftime("%Y-%m-%d"),
            "limit":     history_days + 30,
            "feed":      "iex",
        }
        r = requests.get(DATA_URL.format(symbol), headers=HEADERS,
                         params=params, timeout=10)
        if r.status_code != 200:
            return None, None
        bars = r.json().get("bars", [])
        if len(bars) < 60:
            return None, None
        closes = [b["c"] for b in bars]
    except Exception:
        return None, None

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
      - Bump size_multiplier from 0.5 -> 0.85
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
        et    = pytz.timezone("America/New_York")
        end   = datetime.now(et)
        start = end - timedelta(days=lookback_days + 10)
        params = {
            "timeframe": "1Day",
            "start":     start.strftime("%Y-%m-%d"),
            "end":       end.strftime("%Y-%m-%d"),
            "limit":     lookback_days + 5,
            "feed":      "iex",
        }
        r = requests.get(DATA_URL.format(symbol), headers=HEADERS,
                         params=params, timeout=10)
        if r.status_code != 200:
            return None
        bars = r.json().get("bars", [])
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
    try:
        import yfinance as yf
    except ImportError:
        return None

    try:
        tickers = yf.Tickers("^VIX9D ^VIX ^VIX3M")
        out = {}
        for tk in ("^VIX9D", "^VIX", "^VIX3M"):
            hist = tickers.tickers[tk].history(period="5d", interval="1d")
            if hist is None or hist.empty:
                return None
            out[tk] = float(hist["Close"].iloc[-1])
    except Exception as e:
        print("[regime] term-structure fetch failed: {}".format(e))
        return None

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


def adjust_position_size(base_contracts, regime_data=None):
    """
    Apply regime-based size multiplier. Always returns at least 1 contract.
    """
    if regime_data is None:
        regime_data = classify_regime()
    mult = regime_data["rules"].get("size_multiplier", 1.0)
    return max(1, int(round(base_contracts * mult)))
