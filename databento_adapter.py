# databento_adapter.py
# Databento data adapter using patterns from Databento's official docs/examples.
#
# Provides:
#   - VIX proxy via front-month VX futures (XCBF.PITCH) — VIX index not sold
#   - Overnight CME futures bars (GLBX.MDP3)
#   - Daily options statistics with OI (OPRA.PILLAR)
#
# Auth: set DATABENTO_API_KEY env var.

import os
import json
import sqlite3
from datetime import datetime, timedelta, time as dtime
import pytz

DATABENTO_KEY = os.getenv("DATABENTO_API_KEY", "").strip()

try:
    import databento as _db
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False


_client = None


def _get_client():
    """Lazily instantiate the historical client."""
    global _client
    if not _SDK_AVAILABLE or not DATABENTO_KEY:
        return None
    if _client is None:
        try:
            _client = _db.Historical(DATABENTO_KEY)
        except Exception as e:
            print("[databento] client init failed: {}".format(e))
            _client = None
    return _client


def is_available():
    """Returns True if SDK is installed AND a key is configured."""
    return _SDK_AVAILABLE and bool(DATABENTO_KEY)


# =============================================
# LOCAL CACHE
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
# VIX SPOT PROXY (via VX front-month futures)
# =============================================
#
# Databento does not sell the VIX spot index value. VX futures on XCBF.PITCH
# track spot VIX with a small contango premium that's acceptable for regime
# classification.

def get_vix_spot():
    """
    Returns latest VIX proxy (front-month VX futures close), or None.
    """
    client = _get_client()
    if not client:
        return None

    cached = _cache_get("vix_spot", max_age_seconds=300)
    if cached is not None:
        return cached.get("vix")

    et = pytz.timezone("America/New_York")
    today = datetime.now(et).date()
    start = today - timedelta(days=7)

    try:
        df = client.timeseries.get_range(
            dataset  = "XCBF.PITCH",
            symbols  = ["VX.c.0"],
            stype_in = "continuous",
            schema   = "ohlcv-1d",
            start    = start.isoformat(),
            end      = today.isoformat(),
        ).to_df()

        if df is None or df.empty:
            print("[databento] VX futures returned empty - is XCBF.PITCH activated?")
            return None

        vx = float(df.iloc[-1]["close"])
        if vx < 8 or vx > 100:
            print("[databento] VX out of range: {}".format(vx))
            return None

        _cache_set("vix_spot", {"vix": vx})
        return vx

    except Exception as e:
        print("[databento] VX fetch failed ({}): {}".format(type(e).__name__, e))
        return None


# =============================================
# OVERNIGHT FUTURES
# =============================================

CONTRACT_MAP = {
    "ES":  "ES.n.0",
    "NQ":  "NQ.n.0",
    "RTY": "RTY.n.0",
    "YM":  "YM.n.0",
}


def get_overnight_bars(contract="ES", target_date_et=None):
    """
    Fetch hourly bars for the overnight session leading into target_date_et.
    Returns list of bars: [{t, o, h, l, c, v}, ...]
    """
    client = _get_client()
    if not client:
        return []

    et = pytz.timezone("America/New_York")
    if target_date_et is None:
        target_date_et = datetime.now(et).date()

    prev = target_date_et - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)

    start_iso = (prev.isoformat() + "T20:00:00")
    end_iso   = (target_date_et.isoformat() + "T14:30:00")

    cache_key = "on_{}_{}".format(contract, target_date_et.isoformat())
    cached = _cache_get(cache_key, max_age_seconds=3600)
    if cached:
        return cached.get("bars", [])

    symbol = CONTRACT_MAP.get(contract, "ES.n.0")
    try:
        df = client.timeseries.get_range(
            dataset  = "GLBX.MDP3",
            symbols  = [symbol],
            stype_in = "continuous",
            schema   = "ohlcv-1h",
            start    = start_iso,
            end      = end_iso,
        ).to_df()

        if df is None or df.empty:
            print("[databento] {} overnight empty. Window: {} -> {}".format(
                contract, start_iso, end_iso))
            return []

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
        print("[databento] {} overnight failed ({}): {}".format(
            contract, type(e).__name__, e))
        return []


# =============================================
# OPTIONS CHAIN with OI (for GEX)
# =============================================

def get_options_chain_snapshot(underlying, target_date_et=None,
                                 expiries_ahead=3):
    """
    Returns EOD options chain with open interest.
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

    start_iso = (target_date_et - timedelta(days=2)).isoformat()
    end_iso   = target_date_et.isoformat()
    parent    = underlying + ".OPT"

    try:
        def_df = client.timeseries.get_range(
            dataset  = "OPRA.PILLAR",
            symbols  = [parent],
            stype_in = "parent",
            schema   = "definition",
            start    = start_iso,
            end      = end_iso,
        ).to_df()

        if def_df is None or def_df.empty:
            print("[databento] {} definitions empty".format(underlying))
            return []

        inst_meta = {}
        for _, row in def_df.iterrows():
            try:
                iid = int(row.get("instrument_id"))
                strike = row.get("strike_price")
                if strike is None:
                    continue
                strike = float(strike)
                if strike > 100000:
                    strike = strike / 1e9

                expiry = row.get("expiration")
                if expiry is not None and hasattr(expiry, "date"):
                    expiry_str = expiry.date().isoformat()
                elif expiry:
                    expiry_str = str(expiry)[:10]
                else:
                    continue

                inst_class = str(row.get("instrument_class", "")).upper()
                opt_type = "call" if "C" in inst_class else \
                           "put"  if "P" in inst_class else None
                if not opt_type:
                    continue

                inst_meta[iid] = {
                    "strike": strike,
                    "expiry": expiry_str,
                    "type":   opt_type,
                }
            except Exception:
                continue

        if not inst_meta:
            print("[databento] {} no parseable definitions".format(underlying))
            return []

        stats_df = client.timeseries.get_range(
            dataset  = "OPRA.PILLAR",
            symbols  = [parent],
            stype_in = "parent",
            schema   = "statistics",
            start    = start_iso,
            end      = end_iso,
        ).to_df()

        if stats_df is None or stats_df.empty:
            print("[databento] {} statistics empty".format(underlying))
            return []

        if "stat_type" in stats_df.columns:
            oi_df = stats_df[stats_df["stat_type"] == 9]
        else:
            oi_df = stats_df

        oi_by_inst = {}
        for _, row in oi_df.iterrows():
            try:
                iid = int(row.get("instrument_id"))
                qty = int(row.get("quantity", 0))
                if qty > 0:
                    oi_by_inst[iid] = qty
            except Exception:
                continue

        chain = []
        for iid, oi in oi_by_inst.items():
            meta = inst_meta.get(iid)
            if not meta:
                continue
            chain.append({
                "strike":              meta["strike"],
                "expiry":              meta["expiry"],
                "type":                meta["type"],
                "open_interest":       oi,
                "implied_volatility":  None,
            })

        if chain:
            chain.sort(key=lambda x: x["expiry"])
            unique_exp = sorted(set(c["expiry"] for c in chain))
            keep = set(unique_exp[:expiries_ahead])
            chain = [c for c in chain if c["expiry"] in keep]

        _cache_set(cache_key, {"chain": chain})
        return chain

    except Exception as e:
        print("[databento] {} chain failed ({}): {}".format(
            underlying, type(e).__name__, e))
        return []


# =============================================
# DIAGNOSTICS
# =============================================

def list_available_datasets():
    """Returns list of dataset IDs the account can access."""
    client = _get_client()
    if not client:
        return []
    try:
        return list(client.metadata.list_datasets())
    except Exception as e:
        print("[databento] list_datasets failed: {}".format(e))
        return []


def get_cost_estimate(dataset, symbols, schema, start, end, stype_in="continuous"):
    """Returns estimated USD cost for a query before running it."""
    client = _get_client()
    if not client:
        return None
    try:
        cost = client.metadata.get_cost(
            dataset=dataset, symbols=symbols, schema=schema,
            start=start, end=end, stype_in=stype_in,
        )
        return float(cost)
    except Exception as e:
        return {"error": str(e)}


def diagnostic():
    """Comprehensive status check."""
    out = {
        "sdk_installed": _SDK_AVAILABLE,
        "key_set":       bool(DATABENTO_KEY),
        "available":     is_available(),
    }
    if not is_available():
        if not _SDK_AVAILABLE:
            out["note"] = "Install databento SDK: pip install databento"
        elif not DATABENTO_KEY:
            out["note"] = "Set DATABENTO_API_KEY env var."
        return out

    out["accessible_datasets"] = list_available_datasets()

    try:
        vix = get_vix_spot()
        out["vix"] = vix
    except Exception as e:
        out["vix_error"] = str(e)
    try:
        bars = get_overnight_bars("ES")
        out["es_overnight_bars"] = len(bars) if bars else 0
        if bars:
            out["es_overnight_last"] = bars[-1]
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
