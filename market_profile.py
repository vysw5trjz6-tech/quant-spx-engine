# market_profile.py
# Volume-based Market Profile / TPO for ES futures.
#
# Provides the key auction-theory levels every CME trader watches:
#   - POC (Point of Control)     : single most-traded price of the prior session
#   - VAH (Value Area High)      : top of the 70% volume zone
#   - VAL (Value Area Low)       : bottom of the 70% volume zone
#   - Single Prints              : TPO blocks with no overlap (magnet levels)
#
# Strategy use:
#   - Open above prior VAH = "Open Drive" continuation setup (Dalton)
#   - Open inside prior VA = balance/rotation day, expect VAH/VAL tests
#   - Single prints below act as gap-fill targets
#   - VAH/VAL flip to support/resistance once broken
#
# Data: requires GLBX.MDP3 ES 5-min or 30-min bars from Databento.
# Cost impact: queries ~5KB per session = negligible.

import os
import sqlite3
import db_utils
import json
import statistics
from datetime import datetime, timedelta, time as dtime
import pytz

PROFILE_DB = "market_profile.db"


def _init_db():
    conn = db_utils.connect(PROFILE_DB)
    c    = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            symbol      TEXT NOT NULL,
            session_date TEXT NOT NULL,
            session_type TEXT NOT NULL,   -- 'RTH' or 'ETH'
            poc         REAL,
            vah         REAL,
            val         REAL,
            session_high REAL,
            session_low  REAL,
            total_volume INTEGER,
            single_prints TEXT,           -- JSON array of single-print prices
            stored_at   TEXT,
            PRIMARY KEY (symbol, session_date, session_type)
        )
    """)
    conn.commit()
    conn.close()


_init_db()


# =============================================
# CORE PROFILE COMPUTATION
# =============================================

def compute_volume_profile(bars, price_bucket_size=0.25):
    """
    Build a volume-by-price histogram from a list of bars, then compute
    POC and Value Area.

    Args:
      bars: list of {o, h, l, c, v} dicts (CME ES 5-min or 30-min works)
      price_bucket_size: granularity in points. ES tick = 0.25.

    Returns dict with poc, vah, val, total_volume, single_prints.
    """
    if not bars:
        return None

    # Build histogram: bucket → total volume traded in that bucket
    # We distribute each bar's volume across all buckets it touched.
    buckets = {}
    for bar in bars:
        try:
            high = float(bar["h"])
            low  = float(bar["l"])
            vol  = int(bar.get("v", 0))
        except (KeyError, ValueError, TypeError):
            continue
        if vol <= 0 or high < low:
            continue

        # Round to nearest tick
        lo_bucket = round(low  / price_bucket_size) * price_bucket_size
        hi_bucket = round(high / price_bucket_size) * price_bucket_size

        # Number of buckets this bar spans
        n_buckets = int(round((hi_bucket - lo_bucket) / price_bucket_size)) + 1
        if n_buckets <= 0:
            continue
        vol_per_bucket = vol / n_buckets

        for i in range(n_buckets):
            b = round(lo_bucket + i * price_bucket_size, 2)
            buckets[b] = buckets.get(b, 0) + vol_per_bucket

    if not buckets:
        return None

    total_volume = sum(buckets.values())
    if total_volume <= 0:
        return None

    # POC: highest-volume bucket
    poc_bucket, poc_volume = max(buckets.items(), key=lambda x: x[1])

    # Value Area: expand around POC until 70% of volume is captured
    target_volume = total_volume * 0.70
    sorted_buckets = sorted(buckets.keys())

    # Start at POC, expand outward
    va_buckets = [poc_bucket]
    va_volume  = poc_volume
    lo_idx = sorted_buckets.index(poc_bucket)
    hi_idx = lo_idx

    while va_volume < target_volume:
        # Look at the bucket just above the current VA and the one just below
        above_idx = hi_idx + 1
        below_idx = lo_idx - 1

        above_vol = buckets[sorted_buckets[above_idx]] if above_idx < len(sorted_buckets) else 0
        below_vol = buckets[sorted_buckets[below_idx]] if below_idx >= 0 else 0

        if above_vol == 0 and below_vol == 0:
            break

        # Take whichever side has more volume (or both if tied)
        if above_vol >= below_vol and above_idx < len(sorted_buckets):
            va_buckets.append(sorted_buckets[above_idx])
            va_volume += above_vol
            hi_idx = above_idx
        elif below_idx >= 0:
            va_buckets.append(sorted_buckets[below_idx])
            va_volume += below_vol
            lo_idx = below_idx
        else:
            break

    vah = max(va_buckets)
    val = min(va_buckets)

    # Single prints: buckets where volume is tiny relative to neighbors
    # (a "gap" in the profile that often acts as a magnet)
    single_prints = []
    threshold = total_volume * 0.001   # less than 0.1% of total
    for b in sorted_buckets:
        if val <= b <= vah and buckets[b] < threshold:
            single_prints.append(b)

    return {
        "poc":            round(poc_bucket, 2),
        "vah":            round(vah, 2),
        "val":            round(val, 2),
        "session_high":   round(max(sorted_buckets), 2),
        "session_low":    round(min(sorted_buckets), 2),
        "total_volume":   int(total_volume),
        "single_prints":  [round(p, 2) for p in single_prints],
    }


# =============================================
# BUILD + STORE
# =============================================

def build_rth_profile(symbol="ES", target_date_et=None):
    """
    Build the RTH (9:30 AM – 4:00 PM ET) volume profile for a session.

    Uses Databento's ES 5-min bars via the existing adapter.
    """
    try:
        import databento_adapter
    except ImportError:
        return None

    if not databento_adapter.is_available():
        return None

    et = pytz.timezone("America/New_York")
    if target_date_et is None:
        target_date_et = datetime.now(et).date()
        # Don't build profile for today before RTH close
        now = datetime.now(et)
        if now.hour < 16:
            target_date_et = target_date_et - timedelta(days=1)
            while target_date_et.weekday() >= 5:
                target_date_et -= timedelta(days=1)

    cache_key = "rth_{}_{}".format(symbol, target_date_et.isoformat())
    # Check if we already have this profile stored
    existing = load_profile(symbol, target_date_et, "RTH")
    if existing:
        return existing

    # Fetch RTH bars (9:30 AM – 4:00 PM ET = 13:30–20:00 UTC roughly)
    client = databento_adapter._get_client()
    if not client:
        return None

    rth_start = (target_date_et.isoformat() + "T13:30:00")
    rth_end   = (target_date_et.isoformat() + "T20:00:00")

    contract = databento_adapter.CONTRACT_MAP.get(symbol, "ES.n.0")

    try:
        df = client.timeseries.get_range(
            dataset  = "GLBX.MDP3",
            symbols  = [contract],
            stype_in = "continuous",
            schema   = "ohlcv-1m",
            start    = rth_start,
            end      = rth_end,
        ).to_df()

        if df is None or df.empty:
            return None

        bars = []
        for _, row in df.iterrows():
            bars.append({
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
                "v": int(row.get("volume", 0)),
            })
    except Exception as e:
        print("[mprofile] {} RTH fetch failed: {}".format(symbol, e))
        return None

    profile = compute_volume_profile(bars, price_bucket_size=0.25)
    if not profile:
        return None

    profile["symbol"] = symbol
    profile["session_date"] = target_date_et.isoformat()
    profile["session_type"] = "RTH"

    _store_profile(profile)
    return profile


def _store_profile(profile):
    conn = db_utils.connect(PROFILE_DB)
    c    = conn.cursor()
    et = pytz.timezone("America/New_York")
    c.execute("""
        INSERT OR REPLACE INTO profiles
        (symbol, session_date, session_type, poc, vah, val,
         session_high, session_low, total_volume, single_prints, stored_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        profile["symbol"],
        profile["session_date"],
        profile["session_type"],
        profile["poc"], profile["vah"], profile["val"],
        profile["session_high"], profile["session_low"],
        profile["total_volume"],
        json.dumps(profile.get("single_prints", [])),
        datetime.now(et).isoformat(),
    ))
    conn.commit()
    conn.close()


def load_profile(symbol, session_date, session_type="RTH"):
    """Load a stored profile, or None if not present."""
    if hasattr(session_date, "isoformat"):
        session_date = session_date.isoformat()
    conn = db_utils.connect(PROFILE_DB)
    c    = conn.cursor()
    c.execute("""
        SELECT poc, vah, val, session_high, session_low,
               total_volume, single_prints
        FROM profiles
        WHERE symbol = ? AND session_date = ? AND session_type = ?
    """, (symbol, session_date, session_type))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "symbol":         symbol,
        "session_date":   session_date,
        "session_type":   session_type,
        "poc":            row[0],
        "vah":            row[1],
        "val":            row[2],
        "session_high":   row[3],
        "session_low":    row[4],
        "total_volume":   row[5],
        "single_prints":  json.loads(row[6] or "[]"),
    }


# =============================================
# OPENING TYPE CLASSIFICATION (Dalton)
# =============================================
#
# Where today's open sits relative to YESTERDAY's value area tells you
# what KIND of day to expect:
#
#   OPEN_DRIVE_UP    : open above prior VAH → trend day up likely
#   OPEN_DRIVE_DOWN  : open below prior VAL → trend day down likely
#   OPEN_INSIDE_VA   : open within prior VA → rotation/balance day
#   OPEN_REJECT_HI   : open above prior VAH but rejected back in
#   OPEN_REJECT_LO   : open below prior VAL but rejected back in

def classify_opening(today_open, prior_profile):
    """
    Returns classification + expected behavior + grade contribution.

    Args:
      today_open: today's RTH open price
      prior_profile: load_profile() result for yesterday's RTH
    """
    if not prior_profile or today_open is None:
        return None

    poc = prior_profile["poc"]
    vah = prior_profile["vah"]
    val = prior_profile["val"]

    if today_open is None:
        return None

    if today_open > vah:
        # Above prior VAH — drive day setup
        # Distance from VAH matters: >0.5% = strong drive
        dist_pct = (today_open - vah) / vah * 100
        if dist_pct > 0.5:
            return {
                "class":      "OPEN_DRIVE_UP",
                "bias":       "BULL",
                "note":       "Open {:.2f}% above prior VAH {} — drive setup".format(
                    dist_pct, vah),
                "trade_idea": "Long bias. VAH ({}) is now support.".format(vah),
                "grade_pts":  12,
            }
        else:
            return {
                "class":      "OPEN_ABOVE_VAH",
                "bias":       "BULL",
                "note":       "Open just above VAH {} — watch for rejection".format(vah),
                "trade_idea": "Long if VAH holds; short if breaks back below.",
                "grade_pts":  6,
            }
    elif today_open < val:
        dist_pct = (val - today_open) / val * 100
        if dist_pct > 0.5:
            return {
                "class":      "OPEN_DRIVE_DOWN",
                "bias":       "BEAR",
                "note":       "Open {:.2f}% below prior VAL {} — drive setup".format(
                    dist_pct, val),
                "trade_idea": "Short bias. VAL ({}) is now resistance.".format(val),
                "grade_pts":  12,
            }
        else:
            return {
                "class":      "OPEN_BELOW_VAL",
                "bias":       "BEAR",
                "note":       "Open just below VAL {} — watch for rejection".format(val),
                "trade_idea": "Short if VAL holds; long if breaks back above.",
                "grade_pts":  6,
            }
    else:
        # Open inside the VA — rotation day
        # Where exactly in the VA matters
        if today_open >= poc:
            return {
                "class":      "OPEN_INSIDE_VA_UPPER",
                "bias":       "NEUTRAL_BULL",
                "note":       "Open inside VA, above POC ({}) — rotation day".format(poc),
                "trade_idea": "Fade extremes. VAH {} = target, POC {} = stop ref.".format(vah, poc),
                "grade_pts":  3,
            }
        else:
            return {
                "class":      "OPEN_INSIDE_VA_LOWER",
                "bias":       "NEUTRAL_BEAR",
                "note":       "Open inside VA, below POC ({}) — rotation day".format(poc),
                "trade_idea": "Fade extremes. VAL {} = target, POC {} = stop ref.".format(val, poc),
                "grade_pts":  3,
            }


def get_key_levels(symbol="ES", price=None):
    """
    Returns a sorted list of nearby profile-derived levels for today.
    Use as supplement to PDH/PDL in the scanner.
    """
    et = pytz.timezone("America/New_York")
    today = datetime.now(et).date()
    yesterday = today - timedelta(days=1)
    while yesterday.weekday() >= 5:
        yesterday -= timedelta(days=1)

    profile = load_profile(symbol, yesterday, "RTH")
    if not profile:
        return []

    levels = [
        {"name": "POC", "price": profile["poc"], "type": "magnet"},
        {"name": "VAH", "price": profile["vah"], "type": "resistance"},
        {"name": "VAL", "price": profile["val"], "type": "support"},
    ]
    for sp in profile.get("single_prints", []):
        levels.append({"name": "Single Print", "price": sp, "type": "magnet"})

    if price is not None:
        for L in levels:
            L["dist_pct"] = round((L["price"] - price) / price * 100, 3)
        levels.sort(key=lambda x: abs(x.get("dist_pct", 99)))

    return levels
