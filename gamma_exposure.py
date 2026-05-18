# gamma_exposure.py
# Dealer Gamma Exposure (GEX) — the most important regime indicator
# you don't currently have.
#
# Concept: market makers run delta-neutral. When customers buy options,
# dealers go SHORT gamma. Short-gamma dealers must SELL into rallies and
# BUY into dips — this dampens moves and produces mean-reverting tape.
# Long-gamma dealers do the opposite: chase rallies, sell dips → trending tape.
#
# Practical use:
#   GEX > 0  (long gamma)  → buy dips to VWAP, fade extremes, range trade
#   GEX < 0  (short gamma) → trend continues, breakouts work, no mean reversion
#   |GEX| > $5B            → very strong regime
#   GEX flips intraday     → volatility expansion incoming
#
# Implementation: compute GEX from CBOE option chain end-of-day OI.
# Free source: CBOE delayed data. Paid: SpotGamma, MenthorQ, Polygon options.

import os
import math
import json
import sqlite3
import db_utils
from datetime import datetime, timedelta
import pytz



# =============================================
# BLACK-SCHOLES GAMMA
# =============================================
#
# We compute gamma per contract because the option provider may not give it.
# All we need: spot, strike, days to expiry, IV, risk-free rate.

def _norm_pdf(x):
    return math.exp(-x*x/2.0) / math.sqrt(2.0 * math.pi)


def bs_gamma(spot, strike, dte_years, iv, r=0.045):
    """
    Black-Scholes gamma per share.
    Multiply by 100 for per-contract, then by OI for aggregate.
    """
    if spot <= 0 or strike <= 0 or dte_years <= 0 or iv <= 0:
        return 0.0
    sqrt_t = math.sqrt(dte_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * dte_years) / (iv * sqrt_t)
    return _norm_pdf(d1) / (spot * iv * sqrt_t)


# =============================================
# GEX CALCULATION
# =============================================
#
# Aggregate dealer gamma:
#   For calls: dealers are short, so GEX = - call_gamma * OI * 100 * spot^2 * 0.01
#   For puts:  dealers are long,  so GEX = + put_gamma  * OI * 100 * spot^2 * 0.01
#
# (Sign convention from SpotGamma / Squeezemetrics methodology.)

def compute_gex_from_chain(chain_data, spot_price):
    """
    Compute total dealer GEX from option chain data.

    chain_data: list of dicts with keys:
        strike, expiry (date), type ('call'/'put'),
        open_interest, implied_volatility (decimal, e.g. 0.18 for 18%)

    Returns dict:
      total_gex: aggregate dollar gamma
      gex_by_strike: dict of strike -> gex (top concentration zones)
      zero_gamma_strike: strike at which GEX flips sign (the "flip level")
      call_wall: highest positive GEX strike above spot (resistance)
      put_wall:  highest negative GEX strike below spot (support)
    """
    if not chain_data or spot_price is None:
        return None

    et   = pytz.timezone("America/New_York")
    today = datetime.now(et).date()

    by_strike = {}
    total = 0.0

    for c in chain_data:
        try:
            strike = float(c["strike"])
            expiry = c["expiry"]
            if isinstance(expiry, str):
                expiry = datetime.strptime(expiry[:10], "%Y-%m-%d").date()
            opt_type = (c.get("type") or "").lower()
            oi       = int(c.get("open_interest") or 0)
            iv       = float(c.get("implied_volatility") or 0)
        except (KeyError, ValueError, TypeError):
            continue

        if oi == 0 or iv <= 0 or iv > 5.0:
            continue

        dte_days  = (expiry - today).days
        if dte_days < 0:
            continue
        # Use minimum 0.5 days to avoid div-by-zero on 0DTE
        dte_years = max(dte_days, 0.5) / 365.0

        gamma = bs_gamma(spot_price, strike, dte_years, iv)

        # Dealer-perspective sign convention
        # Calls: customers long → dealers short → negative gamma contribution
        # Puts:  customers long → dealers short → also negative
        # SpotGamma convention: calls add to GEX, puts subtract
        # (assumes net call buying, which is empirical norm)
        sign = 1 if opt_type == "call" else -1

        # Dollar gamma per 1% spot move
        dollar_gamma = gamma * oi * 100 * (spot_price ** 2) * 0.01 * sign

        by_strike[strike] = by_strike.get(strike, 0.0) + dollar_gamma
        total += dollar_gamma

    if not by_strike:
        return None

    # Find zero-gamma flip strike (cumulative GEX crosses zero)
    sorted_strikes = sorted(by_strike.keys())
    cumulative = 0.0
    flip_strike = None
    prev_strike = sorted_strikes[0]
    for k in sorted_strikes:
        prev_cum = cumulative
        cumulative += by_strike[k]
        if prev_cum < 0 and cumulative >= 0:
            flip_strike = (prev_strike + k) / 2.0
            break
        if prev_cum > 0 and cumulative <= 0:
            flip_strike = (prev_strike + k) / 2.0
            break
        prev_strike = k

    # Call wall: largest positive GEX strike above spot
    call_wall = None
    call_wall_gex = 0
    for k, gex in by_strike.items():
        if k > spot_price and gex > call_wall_gex:
            call_wall_gex = gex
            call_wall = k

    # Put wall: largest negative GEX strike below spot
    put_wall = None
    put_wall_gex = 0
    for k, gex in by_strike.items():
        if k < spot_price and gex < put_wall_gex:
            put_wall_gex = gex
            put_wall = k

    return {
        "total_gex":          round(total, 0),
        "total_gex_billions": round(total / 1e9, 2),
        "regime":             "LONG_GAMMA" if total > 0 else "SHORT_GAMMA",
        "zero_gamma_strike":  round(flip_strike, 2) if flip_strike else None,
        "call_wall":          round(call_wall, 2) if call_wall else None,
        "put_wall":           round(put_wall, 2) if put_wall else None,
        "spot":               spot_price,
        "computed_at":        datetime.now(et).isoformat(),
    }


# =============================================
# TENOR-BUCKETED GEX
# =============================================
#
# Aggregate GEX hides the fact that 0DTE dealer hedging flips intraday and
# behaves very differently from monthly positioning. Bucketing by DTE lets
# downstream logic treat them separately -- e.g. weight 0DTE gamma 3x in the
# last hour of trading where it dominates the tape.

TENOR_BUCKETS = (
    ("0DTE", 0,  0),
    ("1-7",  1,  7),
    ("8-30", 8,  30),
    ("30+",  31, 10_000),
)


def compute_gex_by_tenor(chain_data, spot_price):
    """Per-bucket dealer GEX. Returns dict bucket -> {gex, gex_billions, regime}."""
    if not chain_data or spot_price is None:
        return None

    et    = pytz.timezone("America/New_York")
    today = datetime.now(et).date()

    totals = {label: 0.0 for label, _, _ in TENOR_BUCKETS}

    for c in chain_data:
        try:
            strike = float(c["strike"])
            expiry = c["expiry"]
            if isinstance(expiry, str):
                expiry = datetime.strptime(expiry[:10], "%Y-%m-%d").date()
            opt_type = (c.get("type") or "").lower()
            oi       = int(c.get("open_interest") or 0)
            iv       = float(c.get("implied_volatility") or 0)
        except (KeyError, ValueError, TypeError):
            continue

        if oi == 0 or iv <= 0 or iv > 5.0:
            continue

        dte_days = (expiry - today).days
        if dte_days < 0:
            continue
        dte_years = max(dte_days, 0.5) / 365.0

        bucket = None
        for label, lo, hi in TENOR_BUCKETS:
            if lo <= dte_days <= hi:
                bucket = label
                break
        if bucket is None:
            continue

        gamma = bs_gamma(spot_price, strike, dte_years, iv)
        sign  = 1 if opt_type == "call" else -1
        totals[bucket] += gamma * oi * 100 * (spot_price ** 2) * 0.01 * sign

    return {
        label: {
            "gex":          round(totals[label], 0),
            "gex_billions": round(totals[label] / 1e9, 3),
            "regime":       "LONG_GAMMA" if totals[label] > 0 else "SHORT_GAMMA",
        }
        for label in totals
    }


# =============================================
# DATA FETCH — Polygon
# =============================================

def fetch_options_chain(underlying):
    """
    Fetch SPY/QQQ chain for the next several expiries via Databento OPRA.
    Returns a list of {strike, expiry, type, open_interest, implied_volatility}.
    """
    try:
        import databento_adapter
        if databento_adapter.is_available():
            chain = databento_adapter.get_options_chain_snapshot(underlying)
            if chain:
                return chain
    except ImportError:
        pass
    except Exception:
        pass
    return None


# =============================================
# CACHE / STATE
# =============================================

# Persist on the Railway volume (same resolver main.py uses for
# trades.db). A bare relative path lived in the container's ephemeral
# working dir, so every redeploy wiped the GEX snapshot and the bias
# read back as "No GEX data" until the next 4:30 PM ET build.
_DATA_DIR    = (os.getenv("DATA_DIR")
                or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
                or "/tmp").rstrip("/")
GEX_CACHE_DB = _DATA_DIR + "/gex_state.db"


def _init_gex_db():
    conn = db_utils.connect(GEX_CACHE_DB)
    c    = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS gex_snapshots (
            symbol             TEXT NOT NULL,
            snapshot_date      TEXT NOT NULL,
            total_gex          REAL,
            regime             TEXT,
            zero_gamma_strike  REAL,
            call_wall          REAL,
            put_wall           REAL,
            spot               REAL,
            json_blob          TEXT,
            PRIMARY KEY (symbol, snapshot_date)
        )
    """)
    conn.commit()
    conn.close()


_init_gex_db()


def save_gex(symbol, gex_data):
    if not gex_data:
        return
    et = pytz.timezone("America/New_York")
    today = datetime.now(et).strftime("%Y-%m-%d")
    conn = db_utils.connect(GEX_CACHE_DB)
    c    = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO gex_snapshots
        (symbol, snapshot_date, total_gex, regime, zero_gamma_strike,
         call_wall, put_wall, spot, json_blob)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        symbol, today, gex_data["total_gex"], gex_data["regime"],
        gex_data.get("zero_gamma_strike"), gex_data.get("call_wall"),
        gex_data.get("put_wall"), gex_data.get("spot"),
        json.dumps(gex_data),
    ))
    conn.commit()
    conn.close()


def load_latest_gex(symbol):
    conn = db_utils.connect(GEX_CACHE_DB)
    c    = conn.cursor()
    c.execute("""
        SELECT json_blob FROM gex_snapshots
        WHERE symbol = ? ORDER BY snapshot_date DESC LIMIT 1
    """, (symbol,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return json.loads(row[0])


# =============================================
# DAILY BUILD + STRATEGY BIAS
# =============================================

def refresh_gex(symbol="SPY", spot_price=None):
    """
    Run after market close (or before open) to build the day's GEX snapshot.
    Requires either Databento or Polygon key for the chain data.
    """
    # Databento is the sole chain provider.
    try:
        import databento_adapter
        if not databento_adapter.is_available():
            return None
    except ImportError:
        return None

    chain = fetch_options_chain(symbol)
    if not chain or spot_price is None:
        return None
    gex = compute_gex_from_chain(chain, spot_price)
    if gex:
        # Attach tenor breakdown so downstream consumers (get_gex_bias,
        # scanner, dashboard) can read it without re-pulling the chain.
        by_tenor = compute_gex_by_tenor(chain, spot_price)
        if by_tenor:
            gex["gex_by_tenor"] = by_tenor
        save_gex(symbol, gex)
    return gex


def get_gex_bias(symbol="SPY"):
    """
    Returns the strategy bias for today based on dealer GEX regime.
    Use this to flip your scanner between trend-mode and fade-mode.
    """
    bias = _build_gex_bias(symbol)
    # Attach tenor breakdown if the snapshot has it (recent refresh_gex runs).
    snapshot = load_latest_gex(symbol)
    if snapshot and snapshot.get("gex_by_tenor"):
        bias["gex_by_tenor"] = snapshot["gex_by_tenor"]
    return bias


def _build_gex_bias(symbol="SPY"):
    gex = load_latest_gex(symbol)
    if not gex:
        return {
            "regime":      "UNKNOWN",
            "tape_bias":   "TREND",
            "size_mult":   1.0,
            "note":        "No GEX data — defaulting to trend-mode",
        }

    total_b = gex["total_gex"] / 1e9
    regime  = gex["regime"]

    if total_b > 5:
        # Very long gamma → strong mean reversion
        return {
            "regime":      regime,
            "gex_b":       round(total_b, 2),
            "tape_bias":   "STRONG_MEAN_REVERT",
            "size_mult":   0.85,
            "favor":       ["vwap_mr"],
            "avoid":       ["orb", "ib_extension"],
            "note":        "Very long gamma. Range-bound day expected. Fade extremes to VWAP.",
            "call_wall":   gex.get("call_wall"),
            "put_wall":    gex.get("put_wall"),
            "flip":        gex.get("zero_gamma_strike"),
        }
    elif total_b > 1:
        return {
            "regime":      regime,
            "gex_b":       round(total_b, 2),
            "tape_bias":   "MEAN_REVERT",
            "size_mult":   0.95,
            "favor":       ["vwap_mr", "vwap_trend"],
            "avoid":       [],
            "note":        "Moderate long gamma. Tighter ranges. Mean reversion favored.",
            "call_wall":   gex.get("call_wall"),
            "put_wall":    gex.get("put_wall"),
            "flip":        gex.get("zero_gamma_strike"),
        }
    elif total_b > -1:
        return {
            "regime":      "NEUTRAL",
            "gex_b":       round(total_b, 2),
            "tape_bias":   "MIXED",
            "size_mult":   1.0,
            "favor":       [],
            "avoid":       [],
            "note":        "Near zero gamma. Typical mixed regime.",
            "call_wall":   gex.get("call_wall"),
            "put_wall":    gex.get("put_wall"),
            "flip":        gex.get("zero_gamma_strike"),
        }
    elif total_b > -5:
        return {
            "regime":      regime,
            "gex_b":       round(total_b, 2),
            "tape_bias":   "TREND",
            "size_mult":   1.1,
            "favor":       ["orb", "vwap_trend", "ib_extension"],
            "avoid":       ["vwap_mr"],
            "note":        "Short gamma. Trends extend. Breakouts work. No mean reversion.",
            "call_wall":   gex.get("call_wall"),
            "put_wall":    gex.get("put_wall"),
            "flip":        gex.get("zero_gamma_strike"),
        }
    else:
        # Very short gamma → squeeze territory
        return {
            "regime":      regime,
            "gex_b":       round(total_b, 2),
            "tape_bias":   "STRONG_TREND",
            "size_mult":   1.15,
            "favor":       ["orb", "vwap_trend", "ib_extension"],
            "avoid":       ["vwap_mr"],
            "note":        "Very short gamma. Squeeze risk both directions. Wide stops.",
            "call_wall":   gex.get("call_wall"),
            "put_wall":    gex.get("put_wall"),
            "flip":        gex.get("zero_gamma_strike"),
        }


# =============================================
# CLI for daily refresh
# =============================================

if __name__ == "__main__":
    # Pass spot price as arg: python gamma_exposure.py SPY 591.45
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "SPY"
    spot = float(sys.argv[2]) if len(sys.argv) > 2 else None
    if spot is None:
        print("Usage: python gamma_exposure.py SPY <spot_price>")
        sys.exit(1)
    g = refresh_gex(sym, spot)
    print(json.dumps(g, indent=2) if g else "Failed (check DATABENTO_API_KEY)")
