# volume_truth.py
# True bar-of-day volume — replaces the approximation in main.py
#
# Original problem: get_time_vol_ratio() compared current bar volume to a
# fraction of average daily volume. This is wrong because volume profile is
# U-shaped (high at open/close, low at lunch). A 1.5x ratio at noon is
# extraordinary; the same ratio at 9:35 is below average.
#
# Fix: cache 30 days of intraday 5-min bars per symbol, compute the median
# volume for the same bar-of-day slot, return the percentile rank.

import os
import json
import time
import sqlite3
import db_utils
import statistics
import requests
from datetime import datetime, timedelta
import pytz

# Reuse main's Alpaca config
ALPACA_KEY    = os.getenv("APCA_API_KEY_ID", "").strip()
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY", "").strip()
HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}
DATA_URL = "https://data.alpaca.markets/v2/stocks/{}/bars"

VOL_CACHE_DB     = "volume_profile.db"
VOL_LOOKBACK_DAYS = 30
VOL_REFRESH_HOURS = 24   # rebuild cache every 24h

# Profile data version (stored in SQLite user_version). v2: history pulls
# stopped forcing feed=iex and now use the account's default feed -- the
# same feed data_fetcher's live bars come from. IEX prints only a few
# percent of consolidated tape volume, so v1 medians made every live SIP
# bar look 25-100x "Exceptional". A version bump wipes stale rows so
# profiles rebuild on the matching feed.
_PROFILE_VERSION = 2


# =============================================
# CACHE STORAGE
# =============================================

def _init_vol_db():
    conn = db_utils.connect(VOL_CACHE_DB)
    c    = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS bar_profile (
            symbol      TEXT NOT NULL,
            bar_idx     INTEGER NOT NULL,
            median_vol  REAL,
            p25_vol     REAL,
            p75_vol     REAL,
            sample_n    INTEGER,
            updated_at  TEXT,
            PRIMARY KEY (symbol, bar_idx)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS profile_meta (
            symbol      TEXT PRIMARY KEY,
            last_built  TEXT
        )
    """)
    cur_ver = c.execute("PRAGMA user_version").fetchone()[0]
    if cur_ver != _PROFILE_VERSION:
        # Profiles were built under an old (mismatched-feed) scheme --
        # wipe them so needs_refresh() triggers a clean rebuild.
        c.execute("DELETE FROM bar_profile")
        c.execute("DELETE FROM profile_meta")
        c.execute("PRAGMA user_version = {}".format(int(_PROFILE_VERSION)))
    conn.commit()
    conn.close()


_init_vol_db()


# =============================================
# CACHE BUILD
# =============================================

def _fetch_intraday_history(symbol, days_back=VOL_LOOKBACK_DAYS):
    """
    Fetch 5-min bars for the last N trading days.
    Returns list of bars sorted by timestamp.
    """
    end   = datetime.now(pytz.utc)
    start = end - timedelta(days=days_back + 10)  # buffer for weekends

    bars = []
    page_token = None
    pages      = 0
    while pages < 10:
        # No explicit `feed`: use the account's default, which is what
        # data_fetcher's live intraday bars use too. The profile medians
        # MUST come from the same feed as the live volume they're compared
        # against in get_true_volume_ratio, or every ratio is garbage.
        params = {
            "timeframe": "5Min",
            "start":     start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end":       end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit":     10000,
        }
        if page_token:
            params["page_token"] = page_token
        try:
            r = requests.get(DATA_URL.format(symbol), headers=HEADERS,
                             params=params, timeout=15)
            if r.status_code != 200:
                break
            data = r.json()
            bars.extend(data.get("bars", []))
            page_token = data.get("next_page_token")
            if not page_token:
                break
            pages += 1
        except Exception:
            break

    return bars


def _bar_idx_from_timestamp(ts_str):
    """
    Convert ISO timestamp to a 5-min bar index from 9:30 ET.
    9:30 = 0, 9:35 = 1, ..., 15:55 = 77.
    Returns -1 if outside RTH.
    """
    try:
        # Alpaca returns UTC ISO strings
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        et = pytz.timezone("America/New_York")
        dt_et = dt.astimezone(et)
        if dt_et.hour < 9 or (dt_et.hour == 9 and dt_et.minute < 30):
            return -1
        if dt_et.hour >= 16:
            return -1
        minutes_from_open = (dt_et.hour - 9) * 60 + dt_et.minute - 30
        return minutes_from_open // 5
    except Exception:
        return -1


def build_profile(symbol):
    """
    Build the bar-of-day volume profile for a symbol.
    Computes median, p25, p75 volume for each 5-min bar across last 30 days.
    Stores result in SQLite for fast lookup.
    """
    bars = _fetch_intraday_history(symbol)
    if not bars:
        return False

    # Group by bar index
    by_idx = {}
    for b in bars:
        idx = _bar_idx_from_timestamp(b["t"])
        if idx < 0:
            continue
        by_idx.setdefault(idx, []).append(b["v"])

    if not by_idx:
        return False

    et  = pytz.timezone("America/New_York")
    now = datetime.now(et).isoformat()

    conn = db_utils.connect(VOL_CACHE_DB)
    c    = conn.cursor()

    # Wipe and rebuild
    c.execute("DELETE FROM bar_profile WHERE symbol = ?", (symbol,))

    for idx, vols in by_idx.items():
        if len(vols) < 5:
            continue
        vols_sorted = sorted(vols)
        median_v = statistics.median(vols_sorted)
        p25      = vols_sorted[len(vols_sorted) // 4]
        p75      = vols_sorted[(len(vols_sorted) * 3) // 4]
        c.execute("""
            INSERT INTO bar_profile
            (symbol, bar_idx, median_vol, p25_vol, p75_vol, sample_n, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (symbol, idx, median_v, p25, p75, len(vols), now))

    c.execute("""
        INSERT OR REPLACE INTO profile_meta (symbol, last_built)
        VALUES (?, ?)
    """, (symbol, now))
    conn.commit()
    conn.close()
    return True


def needs_refresh(symbol):
    """Returns True if profile is missing or older than VOL_REFRESH_HOURS."""
    conn = db_utils.connect(VOL_CACHE_DB)
    c    = conn.cursor()
    c.execute("SELECT last_built FROM profile_meta WHERE symbol = ?", (symbol,))
    row = c.fetchone()
    conn.close()

    if not row:
        return True
    try:
        last = datetime.fromisoformat(row[0])
        et   = pytz.timezone("America/New_York")
        age_hours = (datetime.now(et) - last).total_seconds() / 3600.0
        return age_hours > VOL_REFRESH_HOURS
    except Exception:
        return True


def refresh_all(symbols):
    """Build/refresh profiles for a list of symbols. Call once at startup
    and once per day. Skips symbols that don't need refresh.

    Returns (built, failed) symbol lists. A symbol in `failed` has no
    usable profile, which silently disables every volume-gated strategy
    for it (get_true_volume_ratio returns the N/A sentinel) -- callers
    should surface that, not just count successes."""
    built  = []
    failed = []
    for sym in symbols:
        if needs_refresh(sym):
            if build_profile(sym):
                built.append(sym)
            else:
                failed.append(sym)
    return built, failed


# =============================================
# LOOKUP — what main.py calls every scan
# =============================================

def get_true_volume_ratio(symbol, current_bar_idx, current_volume):
    """
    Compares current bar volume to the historical median for the same
    bar-of-day slot.

    Returns (ratio, label, percentile_estimate)
      ratio = current_volume / median_volume_for_this_slot
      label = "EXCEPTIONAL" / "ELEVATED" / "NORMAL" / "LIGHT"
      percentile = rough percentile rank using p25/p75 cache (0-100)
    """
    if current_bar_idx < 0 or current_bar_idx > 77:
        return 1.0, "N/A", 50

    conn = db_utils.connect(VOL_CACHE_DB)
    c    = conn.cursor()
    c.execute("""
        SELECT median_vol, p25_vol, p75_vol, sample_n
        FROM bar_profile
        WHERE symbol = ? AND bar_idx = ?
    """, (symbol, current_bar_idx))
    row = c.fetchone()
    conn.close()

    if not row or not row[0] or row[3] < 5:
        # Fallback: no profile yet
        return 1.0, "N/A", 50

    median_v, p25, p75, n = row
    ratio = current_volume / median_v if median_v > 0 else 1.0

    # Rough percentile: if below p25 -> ~12pct, above p75 -> ~87pct
    if current_volume <= p25:
        percentile = max(5, int(50 * current_volume / max(p25, 1)))
    elif current_volume >= p75:
        # Linear extrapolation past p75
        ratio_past_p75 = (current_volume - p75) / max(p75, 1)
        percentile = min(99, 75 + int(ratio_past_p75 * 20))
    else:
        # Between p25 and p75 — interpolate around median
        if current_volume <= median_v:
            percentile = 25 + int(25 * (current_volume - p25) / max(median_v - p25, 1))
        else:
            percentile = 50 + int(25 * (current_volume - median_v) / max(p75 - median_v, 1))

    if ratio >= 2.5:
        label = "EXCEPTIONAL"
    elif ratio >= 1.5:
        label = "ELEVATED"
    elif ratio >= 0.8:
        label = "NORMAL"
    else:
        label = "LIGHT"

    return round(ratio, 2), label, percentile


# =============================================
# CLI for manual rebuild
# =============================================

if __name__ == "__main__":
    syms = ["SPY", "QQQ", "IWM", "AAPL", "NVDA", "TSLA", "AMD", "META"]
    print("Building volume profiles for {} symbols...".format(len(syms)))
    for s in syms:
        ok = build_profile(s)
        print("  {} -> {}".format(s, "OK" if ok else "FAIL"))
