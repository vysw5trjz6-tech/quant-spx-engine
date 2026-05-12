# databento_adapter.py
# Unified Databento data adapter using the official Python SDK.
#
# Provides:
#   - VIX spot price (real CBOE index, not VIXY proxy)
#   - Overnight CME futures bars (ES/NQ/RTY/YM)
#   - Options chain snapshots with OI (for GEX calculation)
#
# Auth: set DATABENTO_API_KEY env var (32-char hex starting with "db-").
#
# SDK docs: https://databento.com/docs/api-reference-historical/basics
#
# Cost considerations:
#   - Historical API charges by data volume (bytes)
#   - We cache aggressively (60s for VIX, 1hr for bars and chains)
#   - Live streams are flat-rate but we don't use them here yet
#   - Typical daily spend at our usage: under $1

import os
import json
import sqlite3
from datetime import datetime, timedelta, time as dtime
import pytz

DATABENTO_KEY = os.getenv("DATABENTO_API_KEY", "").strip()

# Lazy SDK import — module still loads if SDK not installed
try:
    import databento as _db
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False


# Cached client — reuse connection across calls
_client = None


def _get_client():
    """Lazily instantiate the historical client."""
    global _client
    if not _SDK_AVAILABLE or not DATABENTO_KEY:
        return None
    if _client is None:
        try:
            _client = _db.Historical(DATABENTO_KEY)
        except Exception:
            _client = None
    return _client


def is_available():
    """Returns True if SDK is installed AND a key is configured."""
    return _SDK_AVAILABLE and bool(DATABENTO_KEY)


# =============================================
# LOCAL CACHE (so we don't re-bill for same query)
# =============================================

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
    """, (key, json.dumps(value, default=str), datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


# =============================================
# VIX SPOT
# =============================================
#
# Dataset: OPRA.PILLAR (consolidated US options) — VIX is published as an index
# Schema: ohlcv-1m
# Symbol: "VIX" with stype_in="raw_symbol"

def get_vix_spot():
    """
    Returns latest VIX close as float, or None.
    Cached 60s.
    """
    client = _get_client()
    if not client:
        return None

    cached = _cache_get("vix_spot", max_age_seconds=60)
    if cached is not None:
        return cached.get("vix")

    et    = pytz.timezone("America/New_York")
    end   = datetime.now(et)
    start = end - timedelta(minutes=30)

    try:
        df = client.timeseries.get_range(
            dataset  = "OPRA.PILLAR",
            symbols  = ["VIX"],
            stype_in = "raw_symbol",
            schema   = "ohlcv-1m",
            start    = start,
            end      = end,
        ).to_df()

        if df is None or df.empty:
            print("[databento] VIX query returned empty DataFrame")
            return None

        # SDK returns DataFrame with prices already scaled (no manual /1e9)
        vix = float(df.iloc[-1]["close"])

        # Sanity: VIX is conventionally 5–80 outside extreme events
        if vix < 1 or vix > 100:
            print("[databento] VIX out of range: {}".format(vix))
            return None

        _cache_set("vix_spot", {"vix": vix})
        return vix
    except Exception as e:
        print("[databento] VIX fetch failed: {}".format(e))
        return None


# =============================================
# OVERNIGHT FUTURES
# =============================================
#
# Dataset: GLBX.MDP3 (CME Globex full feed)
# Continuous-contract symbology: ES.c.0 = front month ES auto-rolled
# Schema: ohlcv-5m

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
    client = _get_client()
    if not client:
        return []

    et = pytz.timezone("America/New_York")
    if target_date_et is None:
        target_date_et = datetime.now(et).date()

    # Previous trading day
    prev = target_date_et - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)

    start = et.localize(datetime.combine(prev, dtime(16, 15)))
    end   = et.localize(datetime.combine(target_date_et, dtime(9, 30)))

    cache_key = "on_{}_{}".format(contract, target_date_et.isoformat())
    cached = _cache_get(cache_key, max_age_seconds=3600)
    if cached:
        return cached.get("bars", [])

    symbol = CONTRACT_MAP.get(contract, "ES.c.0")
    try:
        df = client.timeseries.get_range(
            dataset  = "GLBX.MDP3",
            symbols  = [symbol],
            stype_in = "continuous",
            schema   = "ohlcv-5m",
            start    = start,
            end      = end,
        ).to_df()

        if df is None or df.empty:
            print("[databento] {} overnight returned empty DataFrame".format(contract))
            return []

        # Convert DataFrame to the same dict shape the rest of the codebase
        # expects from Alpaca bars: {t, o, h, l, c, v}
        bars = []
        for ts, row in df.iterrows():
            bars.append({
                "t": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
                "v": int(row.get("volume", 0)),
            })

        _cache_set(cache_key, {"bars": bars})
        return bars
    except Exception as e:
        print("[databento] {} overnight fetch failed: {}".format(contract, e))
        return []


# =============================================
# OPTIONS CHAIN (for GEX)
# =============================================
#
# Dataset: OPRA.PILLAR (consolidated US options feed)
# Schema: "statistics" gives daily OI per contract
# Symbology: parent-symbol resolution — passing "SPY.OPT" with
# stype_in="parent" returns every SPY option contract.
#
# Returns: list of {strike, expiry, type, open_interest, implied_volatility}

def get_options_chain_snapshot(underlying, target_date_et=None,
                                 expiries_ahead=3):
    """
    Returns EOD options chain for the underlying, covering the next N
    nearest expiries.

    Includes open interest (essential for GEX). IV is left as None; the
    caller computes gamma via Black-Scholes using OI + strike + spot, which
    is the standard SpotGamma-style approach.
    """
    client = _get_client()
    if not client:
        return []

    et = pytz.timezone("America/New_York")
    if target_date_et is None:
        target_date_et = datetime.now(et).date()

    cache_key = "chain_{}_{}".format(underlying, target_date_et.isoformat())
    cached = _cache_get(cache_key, max_age_seconds=3600)
    if cached:
        return cached.get("chain", [])

    # EOD snapshot window
    snap_time = et.localize(datetime.combine(target_date_et, dtime(16, 0)))
    start = snap_time - timedelta(hours=1)
    end   = snap_time + timedelta(minutes=30)

    try:
        df = client.timeseries.get_range(
            dataset  = "OPRA.PILLAR",
            symbols  = [underlying + ".OPT"],
            stype_in = "parent",
            schema   = "statistics",
            start    = start,
            end      = end,
        ).to_df()

        if df is None or df.empty:
            print("[databento] {} chain returned empty DataFrame".format(underlying))
            return []

        # Filter to open-interest records.
        # Databento statistics schema: stat_type=9 is daily open interest.
        if "stat_type" in df.columns:
            oi_records = df[df["stat_type"] == 9]
        else:
            oi_records = df

        chain = []
        for _, row in oi_records.iterrows():
            try:
                strike = float(row.get("strike_price",
                                        row.get("strike", 0)))
                if strike == 0:
                    # Manual parse from raw_symbol fallback
                    sym = str(row.get("raw_symbol", row.get("symbol", "")))
                    if len(sym) < 15:
                        continue
                    cp_idx = max(sym.rfind("C"), sym.rfind("P"))
                    if cp_idx < 6:
                        continue
                    strike = int(sym[cp_idx+1:cp_idx+9]) / 1000.0

                exp_val = row.get("expiration") or row.get("expiry")
                if exp_val is not None and hasattr(exp_val, "date"):
                    expiry = exp_val.date().isoformat()
                elif exp_val:
                    expiry = str(exp_val)[:10]
                else:
                    sym = str(row.get("raw_symbol", row.get("symbol", "")))
                    cp_idx = max(sym.rfind("C"), sym.rfind("P"))
                    yy = int(sym[cp_idx-6:cp_idx-4])
                    mm = int(sym[cp_idx-4:cp_idx-2])
                    dd = int(sym[cp_idx-2:cp_idx])
                    expiry = "{:04d}-{:02d}-{:02d}".format(2000+yy, mm, dd)

                opt_type_raw = row.get("instrument_class") or \
                               row.get("option_type")
                if opt_type_raw:
                    s = str(opt_type_raw).upper()
                    opt_type = "call" if s.startswith("C") else \
                               "put"  if s.startswith("P") else None
                else:
                    sym = str(row.get("raw_symbol", row.get("symbol", "")))
                    cp_idx = max(sym.rfind("C"), sym.rfind("P"))
                    opt_type = "call" if sym[cp_idx] == "C" else "put"

                if not opt_type:
                    continue

                oi = int(row.get("quantity", 0))
                if oi <= 0:
                    continue

                chain.append({
                    "strike":             strike,
                    "expiry":             expiry,
                    "type":               opt_type,
                    "open_interest":      oi,
                    "implied_volatility": None,  # caller uses BS gamma
                })
            except Exception:
                continue

        if chain:
            chain.sort(key=lambda x: x["expiry"])
            unique_exp = sorted(set(c["expiry"] for c in chain))
            keep = set(unique_exp[:expiries_ahead])
            chain = [c for c in chain if c["expiry"] in keep]

        _cache_set(cache_key, {"chain": chain})
        return chain
    except Exception as e:
        print("[databento] {} chain fetch failed: {}".format(underlying, e))
        return []


# =============================================
# DIAGNOSTICS
# =============================================

def diagnostic():
    """Returns status of all 3 capabilities."""
    out = {
        "sdk_installed": _SDK_AVAILABLE,
        "key_set":       bool(DATABENTO_KEY),
        "available":     is_available(),
    }
    if not is_available():
        if not _SDK_AVAILABLE:
            out["note"] = "Install databento SDK: pip install databento"
        elif not DATABENTO_KEY:
            out["note"] = "Set DATABENTO_API_KEY env var to enable."
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
