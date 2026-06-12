# oi_delta.py
# Open Interest delta tracking — compares today's EOD OI vs yesterday's
# at the strike level. Surfaces where institutions BUILT positions overnight.
#
# Why this matters:
#   - OI change is a 24h-leading indicator of institutional positioning
#   - Net call buildup at OTM strikes → bullish hedging or directional bets
#   - Put OI buildup at specific strikes → identifies real downside fears,
#     not just whatever the model would call "support"
#   - Volume without OI change = day-trade noise (closed same day)
#   - Volume WITH OI change = new positions stuck for at least a day
#
# Data source: reuses Databento OPRA.PILLAR chain snapshots we already pull
# for GEX. Storage: daily snapshots persisted to SQLite for diffing.

import os
import sqlite3
import db_utils
import json
import statistics
from datetime import datetime, timedelta
import pytz

OI_DB = db_utils.data_path("oi_history.db")

# Strike-level snapshots add ~100k rows/day across the 72-symbol sweep, and
# compute_delta only ever diffs the last handful of sessions. Keep a quarter
# of history for ad-hoc analysis; drop the rest so the DB doesn't grow by
# hundreds of MB/year on the persistent volume.
OI_RETENTION_DAYS = 90
_last_prune_ymd   = None


def _prune_old(conn, today_ymd):
    """Delete snapshots older than OI_RETENTION_DAYS. At most once per day."""
    global _last_prune_ymd
    if _last_prune_ymd == today_ymd:
        return
    _last_prune_ymd = today_ymd
    try:
        cutoff = (datetime.strptime(today_ymd, "%Y-%m-%d")
                  - timedelta(days=OI_RETENTION_DAYS)).strftime("%Y-%m-%d")
        conn.execute("DELETE FROM oi_snapshots WHERE snap_date < ?", (cutoff,))
    except Exception:
        pass


def _init_db():
    conn = db_utils.connect(OI_DB)
    c    = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS oi_snapshots (
            symbol      TEXT NOT NULL,
            snap_date   TEXT NOT NULL,
            strike      REAL NOT NULL,
            expiry      TEXT NOT NULL,
            opt_type    TEXT NOT NULL,
            oi          INTEGER NOT NULL,
            stored_at   TEXT,
            PRIMARY KEY (symbol, snap_date, strike, expiry, opt_type)
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_oi_lookup
        ON oi_snapshots (symbol, snap_date)
    """)
    # Per-contract option volume (when the chain pull carries it) so we can
    # compute a same-day volume/OI ratio. Added via migration for old DBs.
    try:
        c.execute("ALTER TABLE oi_snapshots ADD COLUMN volume INTEGER")
    except Exception:
        pass
    conn.commit()
    conn.close()


_init_db()


# =============================================
# SNAPSHOT STORAGE
# =============================================

def save_snapshot(symbol, chain_data, snap_date=None):
    """
    Persist today's OI snapshot. `chain_data` is the output of
    databento_adapter.get_options_chain_snapshot(symbol).

    Idempotent: re-running same day overwrites.
    """
    if not chain_data:
        return 0

    et = pytz.timezone("America/New_York")
    if snap_date is None:
        snap_date = datetime.now(et).strftime("%Y-%m-%d")
    now_iso = datetime.now(et).isoformat()

    conn = db_utils.connect(OI_DB)
    c    = conn.cursor()

    # Wipe existing for this date+symbol
    c.execute("DELETE FROM oi_snapshots WHERE symbol = ? AND snap_date = ?",
              (symbol, snap_date))

    rows = 0
    for contract in chain_data:
        try:
            strike   = float(contract["strike"])
            expiry   = str(contract["expiry"])[:10]
            opt_type = contract["type"]
            oi       = int(contract.get("open_interest", 0))
            if oi <= 0:
                continue
            vol = contract.get("volume")
            vol = int(vol) if vol is not None else None
            c.execute("""
                INSERT OR REPLACE INTO oi_snapshots
                (symbol, snap_date, strike, expiry, opt_type, oi, volume, stored_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (symbol, snap_date, strike, expiry, opt_type, oi, vol, now_iso))
            rows += 1
        except Exception:
            continue

    _prune_old(conn, snap_date)
    conn.commit()
    conn.close()
    return rows


# =============================================
# DELTA COMPUTATION
# =============================================

def compute_delta(symbol, current_date=None, lookback_days=1):
    """
    Returns the strike-level OI delta vs lookback_days ago.

    Output:
      {
        "call_oi_today":     total call OI now
        "put_oi_today":      total put OI now
        "call_oi_change":    net change vs yesterday
        "put_oi_change":     net change vs yesterday
        "pc_oi_ratio":       put/call OI ratio
        "pc_change_ratio":   put-change / call-change
        "top_strikes": [     biggest absolute OI changes
          {strike, type, expiry, change, pct_change}
        ],
        "bullish_pressure": -1..+1 directional indicator
      }
    """
    et = pytz.timezone("America/New_York")
    if current_date is None:
        current_date = datetime.now(et).strftime("%Y-%m-%d")

    conn = db_utils.connect(OI_DB)
    c    = conn.cursor()

    # Find the most recent snapshot date AT OR BEFORE current_date
    c.execute("""
        SELECT DISTINCT snap_date FROM oi_snapshots
        WHERE symbol = ? AND snap_date <= ?
        ORDER BY snap_date DESC LIMIT 5
    """, (symbol, current_date))
    dates = [r[0] for r in c.fetchall()]

    if len(dates) < 2:
        conn.close()
        return None  # need at least two snapshots to diff

    today_date = dates[0]
    # Use the N-th prior snapshot (default lookback=1 = yesterday)
    prior_idx = min(lookback_days, len(dates) - 1)
    prior_date = dates[prior_idx]

    # Pull both snapshots
    c.execute("""
        SELECT strike, expiry, opt_type, oi FROM oi_snapshots
        WHERE symbol = ? AND snap_date = ?
    """, (symbol, today_date))
    today_rows = c.fetchall()

    c.execute("""
        SELECT strike, expiry, opt_type, oi FROM oi_snapshots
        WHERE symbol = ? AND snap_date = ?
    """, (symbol, prior_date))
    prior_rows = c.fetchall()
    conn.close()

    if not today_rows or not prior_rows:
        return None

    # Key by (strike, expiry, type)
    today_map = {(r[0], r[1], r[2]): r[3] for r in today_rows}
    prior_map = {(r[0], r[1], r[2]): r[3] for r in prior_rows}

    call_oi_today = sum(v for k, v in today_map.items() if k[2] == "call")
    put_oi_today  = sum(v for k, v in today_map.items() if k[2] == "put")
    call_oi_prior = sum(v for k, v in prior_map.items() if k[2] == "call")
    put_oi_prior  = sum(v for k, v in prior_map.items() if k[2] == "put")

    call_change = call_oi_today - call_oi_prior
    put_change  = put_oi_today  - put_oi_prior

    pc_oi_ratio    = round(put_oi_today / call_oi_today, 3) if call_oi_today > 0 else None
    pc_change_ratio = None
    if call_change != 0:
        pc_change_ratio = round(put_change / call_change, 3) if call_change != 0 else None

    # Strike-level changes — find the biggest absolute movers
    deltas = []
    all_keys = set(today_map.keys()) | set(prior_map.keys())
    for k in all_keys:
        today_oi = today_map.get(k, 0)
        prior_oi = prior_map.get(k, 0)
        change   = today_oi - prior_oi
        if abs(change) < 100:  # noise floor — ignore tiny strikes
            continue
        pct = (change / prior_oi * 100) if prior_oi > 0 else None
        deltas.append({
            "strike":     k[0],
            "expiry":     k[1],
            "type":       k[2],
            "today_oi":   today_oi,
            "prior_oi":   prior_oi,
            "change":     change,
            "pct_change": round(pct, 1) if pct is not None else None,
        })

    deltas.sort(key=lambda x: abs(x["change"]), reverse=True)
    top_strikes = deltas[:10]

    # Bullish pressure: net call build minus put build, normalized
    # Range: -1 (heavy put build) to +1 (heavy call build)
    pressure_raw = call_change - put_change
    total_change_mag = abs(call_change) + abs(put_change)
    if total_change_mag > 0:
        bullish_pressure = round(pressure_raw / total_change_mag, 3)
    else:
        bullish_pressure = 0.0

    return {
        "symbol":             symbol,
        "today_date":         today_date,
        "prior_date":         prior_date,
        "call_oi_today":      call_oi_today,
        "put_oi_today":       put_oi_today,
        "call_oi_change":     call_change,
        "put_oi_change":      put_change,
        "pc_oi_ratio":        pc_oi_ratio,
        "pc_change_ratio":    pc_change_ratio,
        "bullish_pressure":   bullish_pressure,
        "top_strikes":        top_strikes,
    }


# =============================================
# SIGNAL INTERPRETATION
# =============================================

def classify_oi_signal(delta_data, spot_price=None):
    """
    Translates raw delta into a directional signal with confidence.

    Returns:
      {
        "label":      "BULLISH_BUILD" / "BEARISH_BUILD" / "BALANCED" / "STALE",
        "confidence": 0-100
        "note":       human-readable
        "grade_pts":  0-15 contribution to overall grade
      }
    """
    if not delta_data:
        return {
            "label":      "STALE",
            "confidence": 0,
            "note":       "Insufficient OI history (need 2+ days)",
            "grade_pts":  0,
        }

    pressure = delta_data["bullish_pressure"]
    call_chg = delta_data["call_oi_change"]
    put_chg  = delta_data["put_oi_change"]
    total_mag = abs(call_chg) + abs(put_chg)

    # Total magnitude must be meaningful — otherwise it's just churn
    if total_mag < 5000:
        return {
            "label":      "BALANCED",
            "confidence": 30,
            "note":       "Low OI churn — no clear positioning signal",
            "grade_pts":  0,
        }

    # Strong bullish: call OI up + put OI flat/down
    if pressure > 0.4 and call_chg > 0:
        conf = min(95, 50 + int(pressure * 50))
        return {
            "label":      "BULLISH_BUILD",
            "confidence": conf,
            "note":       "Call OI +{:,} vs put OI {:+,} (pressure {:+.2f})".format(
                call_chg, put_chg, pressure),
            "grade_pts":  min(15, int(pressure * 15)),
        }

    # Strong bearish: put OI up + call OI flat/down
    if pressure < -0.4 and put_chg > 0:
        conf = min(95, 50 + int(abs(pressure) * 50))
        return {
            "label":      "BEARISH_BUILD",
            "confidence": conf,
            "note":       "Put OI +{:,} vs call OI {:+,} (pressure {:+.2f})".format(
                put_chg, call_chg, pressure),
            "grade_pts":  min(15, int(abs(pressure) * 15)),
        }

    return {
        "label":      "BALANCED",
        "confidence": 50,
        "note":       "Mixed OI build (calls {:+,} / puts {:+,})".format(call_chg, put_chg),
        "grade_pts":  0,
    }


def get_top_strike_levels(symbol, spot_price=None, max_levels=5):
    """
    Returns the strike levels with biggest OI builds — these act as
    institutional magnet/resistance prices for the day.
    """
    delta = compute_delta(symbol)
    if not delta or not delta.get("top_strikes"):
        return []

    out = []
    for s in delta["top_strikes"][:max_levels]:
        if spot_price:
            pct_from_spot = (s["strike"] - spot_price) / spot_price * 100
        else:
            pct_from_spot = None
        out.append({
            "strike":         s["strike"],
            "type":           s["type"],
            "oi_change":      s["change"],
            "pct_from_spot":  round(pct_from_spot, 2) if pct_from_spot is not None else None,
            "side":           "ABOVE" if (spot_price and s["strike"] > spot_price) else
                              "BELOW" if (spot_price and s["strike"] < spot_price) else None,
        })
    return out


def get_contract_oi_vol(symbol, strike, expiry, opt_type):
    """Latest stored OI (and volume, if captured) for one contract.

    Returns {"oi": int|None, "volume": int|None, "snap_date": str|None} using
    the most recent snapshot for the symbol. Pure DB read -- no network.
    """
    try:
        conn = db_utils.connect(OI_DB)
        c    = conn.cursor()
        c.execute("SELECT MAX(snap_date) FROM oi_snapshots WHERE symbol=?",
                  (symbol,))
        row = c.fetchone()
        snap_date = row[0] if row else None
        if not snap_date:
            conn.close()
            return {"oi": None, "volume": None, "snap_date": None}
        c.execute("""
            SELECT oi, volume FROM oi_snapshots
            WHERE symbol=? AND snap_date=? AND strike=? AND expiry=? AND opt_type=?
        """, (symbol, snap_date, float(strike), str(expiry)[:10], opt_type))
        r = c.fetchone()
        conn.close()
        if not r:
            return {"oi": None, "volume": None, "snap_date": snap_date}
        return {"oi": r[0], "volume": r[1], "snap_date": snap_date}
    except Exception:
        return {"oi": None, "volume": None, "snap_date": None}
