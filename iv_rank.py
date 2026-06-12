# iv_rank.py
# Track daily ATM implied volatility per symbol; compute IV Rank + Percentile.
#
# Why: the earnings IV crush strategy needs IV Rank > 70 to fire. That
# requires a rolling year of daily IV history. This module:
#   1. Pulls today's ATM IV from Alpaca options snapshot
#   2. Stores it in a tiny SQLite cache
#   3. Computes IV Rank = (today - 1yr_min) / (1yr_max - 1yr_min) * 100
#   4. Computes IV Percentile = % of past days where IV was lower than today
#
# Both are reported. IV Rank is more sensitive to outliers (one panic spike
# can suppress today's rank for a year); IV Percentile is more robust.

import os
import sqlite3
import db_utils
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

IV_CACHE_DB = db_utils.data_path("iv_history.db")


def _init_iv_db():
    conn = db_utils.connect(IV_CACHE_DB)
    c    = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS iv_history (
            symbol      TEXT NOT NULL,
            obs_date    TEXT NOT NULL,
            atm_iv      REAL NOT NULL,
            updated_at  TEXT,
            PRIMARY KEY (symbol, obs_date)
        )
    """)
    conn.commit()
    conn.close()


_init_iv_db()


# =============================================
# FETCH ATM IV
# =============================================

def _next_friday_or_monthly():
    """Return the next standard monthly expiry (3rd Friday) as YYYY-MM-DD."""
    et = pytz.timezone("America/New_York")
    today = datetime.now(et).date()
    # Find 3rd Friday of current month
    first = today.replace(day=1)
    days_to_fri = (4 - first.weekday()) % 7
    third_fri = first + timedelta(days=days_to_fri + 14)
    if third_fri <= today:
        # Roll to next month
        if today.month == 12:
            first = today.replace(year=today.year+1, month=1, day=1)
        else:
            first = today.replace(month=today.month+1, day=1)
        days_to_fri = (4 - first.weekday()) % 7
        third_fri = first + timedelta(days=days_to_fri + 14)
    return third_fri.strftime("%Y-%m-%d")


def _candidate_expiries(n=5):
    """
    Return up to N candidate expiries to try for ATM IV lookup, ordered
    by likelihood of having liquid contracts:
      1. Next Friday (weekly)
      2. Friday after that
      3. Next monthly (3rd Friday)
      4. Following monthly
    """
    et    = pytz.timezone("America/New_York")
    today = datetime.now(et).date()
    out   = []

    # Next 4 Fridays
    days_to_fri = (4 - today.weekday()) % 7
    if days_to_fri == 0:
        days_to_fri = 7   # skip today
    for offset in range(0, 28, 7):
        d = today + timedelta(days=days_to_fri + offset)
        out.append(d.strftime("%Y-%m-%d"))

    # Add monthly expiry too
    monthly = _next_friday_or_monthly()
    if monthly not in out:
        out.append(monthly)

    return out[:n]


def fetch_atm_iv(symbol, underlying_price):
    """
    Returns today's ATM IV (average of nearest call + put), or None.

    Tries multiple expiries (next 4 Fridays + next monthly) until one
    returns contracts with valid IVs. Earlier expiries often don't list
    for thinner names; later ones might have stale IV.
    """
    if not ALPACA_KEY or not underlying_price:
        return None

    url = "https://data.alpaca.markets/v1beta1/options/snapshots/{}".format(symbol)
    lo  = round(underlying_price * 0.97, 2)
    hi  = round(underlying_price * 1.03, 2)

    for expiry in _candidate_expiries(5):
        ivs = []
        for opt_type in ("call", "put"):
            try:
                r = requests.get(url, headers=HEADERS, params={
                    "feed":              "indicative",
                    "expiration_date":   expiry,
                    "type":              opt_type,
                    "strike_price_gte":  lo,
                    "strike_price_lte":  hi,
                    "limit":             20,
                }, timeout=10)
                if r.status_code != 200:
                    continue
                snapshots = r.json().get("snapshots", {}) or {}
                if not snapshots:
                    continue

                # Find the strike closest to spot with a valid IV
                best_iv = None
                best_dist = None
                for contract_sym, snap in snapshots.items():
                    try:
                        strike = int(contract_sym[-8:]) / 1000.0
                    except Exception:
                        continue
                    greeks = snap.get("greeks") or {}
                    # impliedVolatility is a TOP-LEVEL field on the Alpaca
                    # options snapshot; reading it from the nested greeks
                    # object always returned None, which is why IV
                    # snapshots stored 0/74 every day. Fall back to the
                    # nested location only for forward-compat / safety.
                    iv = snap.get("impliedVolatility")
                    if iv is None:
                        iv = greeks.get("impliedVolatility")
                    if iv is None or iv <= 0 or iv > 5.0:
                        continue
                    dist = abs(strike - underlying_price)
                    if best_dist is None or dist < best_dist:
                        best_dist = dist
                        best_iv   = iv
                if best_iv:
                    ivs.append(best_iv)
            except Exception:
                continue

        if len(ivs) >= 1:
            return round(statistics.mean(ivs), 4)

    return None


# =============================================
# STORAGE
# =============================================

def store_iv(symbol, iv):
    """Store today's ATM IV. Idempotent — overwrites if same date."""
    if iv is None or iv <= 0:
        return False
    et = pytz.timezone("America/New_York")
    today = datetime.now(et).strftime("%Y-%m-%d")
    now   = datetime.now(et).isoformat()

    conn = db_utils.connect(IV_CACHE_DB)
    c    = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO iv_history (symbol, obs_date, atm_iv, updated_at)
        VALUES (?, ?, ?, ?)
    """, (symbol, today, iv, now))
    conn.commit()
    conn.close()
    return True


def get_history(symbol, lookback_days=252):
    """Return list of (date_str, iv) tuples for the symbol, newest first."""
    conn = db_utils.connect(IV_CACHE_DB)
    c    = conn.cursor()
    c.execute("""
        SELECT obs_date, atm_iv FROM iv_history
        WHERE symbol = ?
        ORDER BY obs_date DESC LIMIT ?
    """, (symbol, lookback_days))
    rows = c.fetchall()
    conn.close()
    return rows


# =============================================
# COMPUTE IV RANK + PERCENTILE
# =============================================

def compute_iv_rank(symbol, today_iv=None, min_history_days=30):
    """
    Returns dict:
      {
        iv_today, iv_rank, iv_percentile,
        iv_min, iv_max, iv_median, samples
      }
    or None if insufficient history.

    iv_rank        = 0..100, where in the range today's IV sits
    iv_percentile  = 0..100, what fraction of history was below today
    """
    history = get_history(symbol, 252)
    if len(history) < min_history_days:
        return None

    ivs = [row[1] for row in history]

    if today_iv is None:
        today_iv = ivs[0]

    iv_min    = min(ivs)
    iv_max    = max(ivs)
    iv_median = statistics.median(ivs)

    if iv_max == iv_min:
        iv_rank = 50.0
    else:
        iv_rank = (today_iv - iv_min) / (iv_max - iv_min) * 100

    below = sum(1 for v in ivs if v < today_iv)
    iv_percentile = below / len(ivs) * 100

    out = {
        "iv_today":      round(today_iv, 4),
        "iv_rank":       round(iv_rank, 1),
        "iv_percentile": round(iv_percentile, 1),
        "iv_min":        round(iv_min, 4),
        "iv_max":        round(iv_max, 4),
        "iv_median":     round(iv_median, 4),
        "samples":       len(ivs),
    }
    # Attach the variance-risk-premium snapshot when realized vol is
    # computable. Cheap: one Alpaca daily-bars call. None on quiet failure.
    try:
        gap = get_rv_iv_gap(symbol)
        if gap:
            out["rv"]            = gap["rv"]
            out["iv_rv_gap_pct"] = gap["gap_pct"]
            out["iv_rv_ratio"]   = gap["ratio"]
            out["vrp_favorable"] = gap["vrp_favorable"]
    except Exception:
        pass
    return out


# =============================================
# REALIZED vs IMPLIED VOL GAP (variance risk premium)
# =============================================

# Threshold for "premium selling is favorable": IV is at least 20% richer
# than 20-day realized vol. Empirically SPY's long-run IV/RV ratio is
# ~1.15-1.25; readings below 1.0 are "vol on sale" (don't sell), and
# readings above 1.4 are crowded fear premium (best sell setups).
VRP_FAVORABLE_RATIO = 1.20


def get_rv_iv_gap(symbol, lookback_days=20):
    """
    Compute the variance risk premium: implied vol minus realized vol.

    Returns dict (decimals -- 0.18 = 18%):
      {
        'iv':            latest stored ATM IV,
        'rv':            trailing N-day realized vol,
        'gap':           iv - rv,
        'gap_pct':       gap expressed in vol points (e.g. 4.5 = 4.5%),
        'ratio':         iv / rv,
        'vrp_favorable': True when ratio >= VRP_FAVORABLE_RATIO,
      }
    or None when either input is missing.

    Use vrp_favorable as a gate on premium-selling setups (short straddles,
    iron condors, call/put-credit spreads). When False, you are paid less
    for vol than the underlying is actually delivering -- a losing trade
    in expectation.
    """
    # Local import to avoid circulars; regime_filter is a sibling module.
    import regime_filter

    rv_pct = regime_filter.get_realized_vol(symbol, lookback_days=lookback_days)
    if rv_pct is None:
        return None
    rv = rv_pct / 100.0  # to decimal

    history = get_history(symbol, 5)
    if not history:
        return None
    iv = history[0][1]

    if rv <= 0 or iv <= 0:
        return None

    gap   = iv - rv
    ratio = iv / rv
    return {
        "iv":            round(iv, 4),
        "rv":            round(rv, 4),
        "gap":           round(gap, 4),
        "gap_pct":       round(gap * 100, 2),
        "ratio":         round(ratio, 3),
        "vrp_favorable": ratio >= VRP_FAVORABLE_RATIO,
    }


# =============================================
# DAILY SNAPSHOT — call from scheduler
# =============================================

def snapshot_symbol(symbol, underlying_price):
    """
    Fetch + store today's IV. Returns the stored value or None.
    Safe to call multiple times per day (last write wins).
    """
    iv = fetch_atm_iv(symbol, underlying_price)
    if iv is None:
        return None
    store_iv(symbol, iv)
    return iv


# =============================================
# CLI
# =============================================

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python iv_rank.py SPY 591.45")
        sys.exit(1)
    sym  = sys.argv[1]
    spot = float(sys.argv[2])
    iv = snapshot_symbol(sym, spot)
    print("Today's ATM IV for {}: {}".format(sym, iv))
    r = compute_iv_rank(sym, iv)
    if r:
        print("IV Rank: {} | IV Percentile: {} | {} samples".format(
            r["iv_rank"], r["iv_percentile"], r["samples"]))
    else:
        print("Insufficient history (need 30+ days)")
