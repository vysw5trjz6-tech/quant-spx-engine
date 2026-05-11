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

# Strategy enable/disable matrix per regime
REGIME_STRATEGY_RULES = {
    "COMPRESSED": {
        "orb":              False,   # ranges too tight
        "vwap_trend":       False,
        "vwap_mr":          True,    # range-bound days favor MR
        "ib_extension":     False,
        "swing_breakout":   False,
        "size_multiplier":  0.5,     # half size — premium bleeds in low IV
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

    Alpaca doesn't carry VIX directly. Two options:
      1. Use VIXY (1-mo VIX futures ETF) as a proxy — close enough for regime
      2. Use Polygon /v2/aggs/ticker/I:VIX/prev for the real VIX

    Default here uses VIXY since you already have Alpaca creds.
    Swap to Polygon if you upgrade.
    """
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
        # Use VIXY as proxy. It tracks 1-mo VIX futures.
        r = requests.get(DATA_URL.format("VIXY"), headers=HEADERS,
                         params=params, timeout=10)
        if r.status_code != 200:
            return None
        bars = r.json().get("bars", [])
        if not bars:
            return None
        # VIXY price ≈ VIX/2.5 historically (rough scaling).
        # Better: use VIXY's percentile rank vs its own 1-yr range.
        # For regime classification, the absolute level matters less than
        # the relative rank. We return VIXY price * empirical scalar.
        return bars[-1]["c"] * 1.85
    except Exception:
        return None


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
