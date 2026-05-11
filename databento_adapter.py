# databento_adapter.py
# Unified Databento data adapter for futures + VIX + options.
#
# Databento (https://databento.com) is a market-data provider with direct
# CME, OPRA, and CBOE feeds. Far better than Polygon/Alpaca for:
#   - ES/NQ/RTY overnight futures
#   - Real-time VIX
#   - Full options chain with OI, IV, greeks
#
# Auth: set DATABENTO_API_KEY env var (32-char hex starting with "db-").
#
# Pricing notes: Databento charges per-byte for historical, flat-rate for
# live streams. For our usage (a few hundred queries/day across overnight
# bars + EOD options snapshots + intraday VIX), the historical API is fine.

import os
import json
import time
import sqlite3
import requests
from datetime import datetime, timedelta
import pytz

DATABENTO_KEY = os.getenv("DATABENTO_API_KEY", "").strip()
DATABENTO_BASE = "https://hist.databento.com/v0"

# Local cache so we don't re-query the same data
_DB_CACHE = "databento_cache.db"


def _init_cache():
    conn = sqlite3.connect(_DB_CACHE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            key       TEXT PRIMARY KEY,
            value     TEXT,
            stored_at TEXT
        )
    """)
    conn.commit()
    conn.close()


_init_cache()


def _cache_get(key, max_age_seconds=300):
    """Return cached JSON value if fresh, else None."""
    conn = sqlite3.connect(_DB_CACHE)
    c    = conn.cursor()
    c.execute("SELECT value, stored_at FROM cache WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    try:
        stored = datetime.fromisoformat(row[1])
        age    = (datetime.utcnow() - stored).total_seconds()
        if age > max_age_seconds:
            return None
        return json.loads(row[0])
    except Exception:
        return None


def _cache_set(key, value):
    conn = sqlite3.connect(_DB_CACHE)
    conn.execute("""
        INSERT OR REPLACE INTO cache (key, value, stored_at)
        VALUES (?, ?, ?)
    """, (key, json.dumps(value), datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def is_available():
    """Returns True if a Databento key is configured."""
    return bool(DATABENTO_KEY)


# =============================================
# VIX SPOT
# =============================================
#
# Databento dataset: CBOE.VIX (live + historical). Symbol: "VIX".
# Schema: trades, ohlcv-1m, ohlcv-1h, ohlcv-1d.
#
# Returns latest CLOSE from a 1-min OHLCV bar.

def get_vix_spot():
    """
    Returns latest VIX close as float, or None.
    Cached 60s to avoid hammering API.
    """
    if not is_available():
        return None

    cached = _cache_get("vix_spot", max_age_seconds=60)
    if cached is not None:
        return cached.get("vix")

    et    = pytz.timezone("America/New_York")
    end   = datetime.now(et)
    start = end - timedelta(minutes=15)

    try:
        # Databento /timeseries.get_range endpoint
        r = requests.get(
            DATABENTO_BASE + "/timeseries.get_range",
            params={
                "dataset":   "OPRA.PILLAR",   # falls back to CBOE indices
                "symbols":   "VIX",
                "stype_in":  "raw_symbol",
                "schema":    "ohlcv-1m",
                "start":     start.isoformat(),
                "end":       end.isoformat(),
                "encoding":  "json",
                "limit":     20,
            },
            auth=(DATABENTO_KEY, ""),
            timeout=10,
        )
        if r.status_code != 200:
            # Try the CBOE dataset directly
            r = requests.get(
                DATABENTO_BASE + "/timeseries.get_range",
                params={
                    "dataset":  "XCBOE.CFE",
                    "symbols":  "VIX",
                    "stype_in": "raw_symbol",
                    "schema":   "ohlcv-1m",
                    "start":    start.isoformat(),
                    "end":      end.isoformat(),
                    "encoding": "json",
                    "limit":    20,
                },
                auth=(DATABENTO_KEY, ""),
                timeout=10,
            )
        if r.status_code != 200:
            return None

        # Databento returns JSONL — one record per line
        lines = [ln for ln in r.text.strip().split("\n") if ln]
        if not lines:
            return None
        last = json.loads(lines[-1])
        # OHLCV price fields are scaled: divide by 1e9 for futures, 1e2 for indices
        # The VIX index uses 1e2 scaling (cents).
        close_raw = last.get("close", 0)
        if isinstance(close_raw, str):
            close_raw = int(close_raw)
        vix = close_raw / 1e9 if close_raw > 1e6 else close_raw / 100.0
        _cache_set("vix_spot", {"vix": vix})
        return vix
    except Exception:
        return None


# =============================================
# OVERNIGHT FUTURES
# =============================================
#
# Continuous-contract symbols (front month auto-rolled):
#   ES.c.0 → S&P E-mini futures
#   NQ.c.0 → Nasdaq 100 E-mini
#   RTY.c.0 → Russell 2000 E-mini
#   YM.c.0 → Dow E-mini
#
# Globex hours: 6 PM ET (Sun-Thu) → 5 PM ET (Mon-Fri), with daily 1hr break.

CONTRACT_MAP = {
    "ES":  "ES.c.0",
    "NQ":  "NQ.c.0",
    "RTY": "RTY.c.0",
    "YM":  "YM.c.0",
}


def get_overnight_bars(contract="ES", target_date_et=None):
    """
    Fetch 5-min bars for the overnight session leading into target_date_et.
    Overnight = previous day's 4:15 PM ET → today's 9:30 AM ET.

    Returns list of bars: [{t, o, h, l, c, v}, ...] in chronological order.
    """
    if not is_available():
        return []

    et = pytz.timezone("America/New_York")
    if target_date_et is None:
        target_date_et = datetime.now(et).date()

    # Previous trading day
    prev = target_date_et - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)

    from datetime import time as dtime
    start = et.localize(datetime.combine(prev, dtime(16, 15)))
    end   = et.localize(datetime.combine(target_date_et, dtime(9, 30)))

    cache_key = "on_{}_{}".format(contract, target_date_et.isoformat())
    cached = _cache_get(cache_key, max_age_seconds=3600)
    if cached:
        return cached.get("bars", [])

    symbol = CONTRACT_MAP.get(contract, "ES.c.0")
    try:
        r = requests.get(
            DATABENTO_BASE + "/timeseries.get_range",
            params={
                "dataset":  "GLBX.MDP3",        # CME Globex
                "symbols":  symbol,
                "stype_in": "continuous",
                "schema":   "ohlcv-5m",
                "start":    start.isoformat(),
                "end":      end.isoformat(),
                "encoding": "json",
                "limit":    500,
            },
            auth=(DATABENTO_KEY, ""),
            timeout=15,
        )
        if r.status_code != 200:
            return []

        bars = []
        for ln in r.text.strip().split("\n"):
            if not ln:
                continue
            try:
                rec = json.loads(ln)
                # Databento OHLCV: prices scaled by 1e9 for futures
                bars.append({
                    "t": rec.get("ts_event") or rec.get("hd", {}).get("ts_event"),
                    "o": rec.get("open", 0)  / 1e9 if rec.get("open", 0)  > 1e6 else rec.get("open"),
                    "h": rec.get("high", 0)  / 1e9 if rec.get("high", 0)  > 1e6 else rec.get("high"),
                    "l": rec.get("low", 0)   / 1e9 if rec.get("low", 0)   > 1e6 else rec.get("low"),
                    "c": rec.get("close", 0) / 1e9 if rec.get("close", 0) > 1e6 else rec.get("close"),
                    "v": rec.get("volume", 0),
                })
            except Exception:
                continue

        _cache_set(cache_key, {"bars": bars})
        return bars
    except Exception:
        return []


# =============================================
# OPTIONS CHAIN (for GEX)
# =============================================
#
# Databento dataset: OPRA.PILLAR — full US options consolidated feed.
# We want the EOD snapshot with open interest. Schema: ohlcv-1d gives
# OHLC but not OI. The "statistics" schema gives OI.
#
# Simpler approach: use the imbalance/statistics schema at end-of-day.
# Returns contracts with: strike, expiry, type, open_interest, last_iv.

def get_options_chain_snapshot(underlying, target_date_et=None,
                                 expiries_ahead=3):
    """
    Returns EOD options chain snapshot for the underlying, covering the
    next N monthly expiries.

    Each contract: {strike, expiry, type, open_interest, implied_volatility}
    """
    if not is_available():
        return []

    et = pytz.timezone("America/New_York")
    if target_date_et is None:
        target_date_et = datetime.now(et).date()

    cache_key = "chain_{}_{}".format(underlying, target_date_et.isoformat())
    cached = _cache_get(cache_key, max_age_seconds=3600)
    if cached:
        return cached.get("chain", [])

    # Databento "definition" schema gives strike/expiry/type
    # "statistics" schema gives daily OI
    # We need to combine. For simplicity, use the parent-symbol resolution:
    #   underlying = "SPY" → returns all SPY options symbols
    from datetime import time as dtime
    snap_time = et.localize(datetime.combine(target_date_et, dtime(16, 0)))
    start = snap_time - timedelta(hours=1)
    end   = snap_time + timedelta(minutes=30)

    try:
        # Fetch the chain definition for the parent
        r = requests.get(
            DATABENTO_BASE + "/timeseries.get_range",
            params={
                "dataset":   "OPRA.PILLAR",
                "symbols":   underlying + ".OPT",
                "stype_in":  "parent",
                "schema":    "statistics",
                "start":     start.isoformat(),
                "end":       end.isoformat(),
                "encoding":  "json",
                "limit":     5000,
            },
            auth=(DATABENTO_KEY, ""),
            timeout=30,
        )
        if r.status_code != 200:
            return []

        # We get statistics records; aggregate OI per contract
        oi_by_sym = {}
        for ln in r.text.strip().split("\n"):
            if not ln: continue
            try:
                rec = json.loads(ln)
                # stat_type 9 = open interest in Databento
                if rec.get("stat_type") == 9:
                    sym = rec.get("symbol") or rec.get("raw_symbol")
                    oi_by_sym[sym] = rec.get("quantity", 0)
            except Exception:
                continue

        # Parse OCC-style symbols: e.g. SPY   240517C00500000
        # Format: ROOT(6) + YYMMDD + C/P + STRIKE*1000(8)
        chain = []
        today = target_date_et
        for sym, oi in oi_by_sym.items():
            try:
                # Skip non-OCC formats
                if len(sym) < 15:
                    continue
                # Find the call/put marker
                cp_idx = max(sym.rfind("C"), sym.rfind("P"))
                if cp_idx < 6: continue
                # Strike is the last 8 digits / 1000
                strike = int(sym[cp_idx+1:cp_idx+9]) / 1000.0
                # Expiry is the 6 digits before C/P
                yy = int(sym[cp_idx-6:cp_idx-4])
                mm = int(sym[cp_idx-4:cp_idx-2])
                dd = int(sym[cp_idx-2:cp_idx])
                expiry = datetime(2000 + yy, mm, dd).date()
                opt_type = "call" if sym[cp_idx] == "C" else "put"

                chain.append({
                    "symbol":        sym.strip(),
                    "strike":        strike,
                    "expiry":        expiry.isoformat(),
                    "type":          opt_type,
                    "open_interest": oi,
                    # IV not in statistics schema; would need NBBO snapshot
                    "implied_volatility": None,
                })
            except Exception:
                continue

        # Filter to next N expiries
        chain.sort(key=lambda x: x["expiry"])
        unique_expiries = sorted(set(c["expiry"] for c in chain))
        keep_expiries   = set(unique_expiries[:expiries_ahead])
        chain = [c for c in chain if c["expiry"] in keep_expiries]

        _cache_set(cache_key, {"chain": chain})
        return chain
    except Exception:
        return []


# =============================================
# DIAGNOSTICS
# =============================================

def diagnostic():
    """Returns status of all 3 capabilities for the /databento endpoint."""
    out = {"available": is_available(), "key_set": bool(DATABENTO_KEY)}
    if not is_available():
        out["note"] = "Set DATABENTO_API_KEY to enable."
        return out
    try:
        vix = get_vix_spot()
        out["vix"] = vix
    except Exception as e:
        out["vix_error"] = str(e)
    try:
        bars = get_overnight_bars("ES")
        out["es_overnight_bars"] = len(bars) if bars else 0
        if bars:
            out["es_overnight_sample"] = bars[-1]
    except Exception as e:
        out["es_error"] = str(e)
    try:
        chain = get_options_chain_snapshot("SPY")
        out["spy_chain_size"] = len(chain) if chain else 0
        if chain:
            out["spy_chain_sample"] = chain[0]
    except Exception as e:
        out["chain_error"] = str(e)
    return out


if __name__ == "__main__":
    import json as _j
    print(_j.dumps(diagnostic(), indent=2, default=str))
