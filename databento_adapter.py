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
import sys
import json
import sqlite3
import db_utils
from datetime import datetime, timedelta, time as dtime, timezone
import pytz


def _utcnow():
    """Timezone-aware UTC now. datetime.utcnow() is deprecated in 3.12+
    and its DeprecationWarning was flooding stderr (→ `error` severity)
    on every scan."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

DATABENTO_KEY = os.getenv("DATABENTO_API_KEY", "").strip()

try:
    import databento as _db
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False


_client = None


# =============================================
# BILLING / 402 CIRCUIT BREAKER
# =============================================
#
# When the account runs out of credit Databento returns 402
# `account_insufficient_funds`. Without a breaker the scanner re-hits the
# same endpoint for every symbol on every scan (50+ tickers × 5min), which
# both spams the logs and wastes any retry budget. After the first billing
# failure we suppress all further Databento calls for _BILLING_COOLDOWN_SECS
# so the scheduler can keep running with fallback data.

_BILLING_COOLDOWN_SECS = 30 * 60
_billing_blocked_until = None  # datetime in UTC, or None


def _is_billing_error(exc):
    s = "{} {}".format(type(exc).__name__, exc).lower()
    return (
        "account_insufficient_funds" in s
        or "insufficient budget" in s
        or "insufficient funds" in s
        or " 402" in s
        or s.startswith("402")
    )


def _billing_blocked():
    return (
        _billing_blocked_until is not None
        and _utcnow() < _billing_blocked_until
    )


_LOG_JSON = os.getenv("LOG_JSON", "").strip() in ("1", "true", "yes")


def _emit_error(event, **fields):
    """Single-line structured stderr write. Mirrors main.log_event so this
    module stays import-cycle-free."""
    ts = _utcnow().strftime("%H:%M:%S")
    if _LOG_JSON:
        payload = {"ts": ts, "level": "error", "event": event}
        payload.update(fields)
        line = json.dumps(payload, default=str)
    else:
        kvs = " ".join("{}={}".format(k, v) for k, v in fields.items())
        line = "[{}] ERROR: {}{}".format(ts, event,
                                          " | " + kvs if kvs else "")
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def _trip_billing_breaker(context):
    global _billing_blocked_until
    first_trip = not _billing_blocked()
    _billing_blocked_until = _utcnow() + timedelta(
        seconds=_BILLING_COOLDOWN_SECS
    )
    if first_trip:
        _emit_error(
            "databento.billing_blocked",
            context=context,
            cooldown_min=_BILLING_COOLDOWN_SECS // 60,
            until=_billing_blocked_until.isoformat() + "Z",
        )


def billing_status():
    """Returns dict with breaker state for the /diagnostic endpoint."""
    return {
        "blocked":       _billing_blocked(),
        "blocked_until": (_billing_blocked_until.isoformat() + "Z")
                         if _billing_blocked_until else None,
    }


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
    """Returns True if SDK is installed, a key is configured, and the
    billing breaker is not currently tripped."""
    if not (_SDK_AVAILABLE and DATABENTO_KEY):
        return False
    if _billing_blocked():
        return False
    return True


# =============================================
# HISTORICAL DAILY OPTIONS (for IVR backfill)
# =============================================
#
# Pulls OPRA.PILLAR definition + ohlcv-1d for a symbol over a date range,
# joined and pre-filtered by DTE so the result fits in memory. The IVR
# backfill picks the closest-to-ATM contract per day from this and solves
# Black-Scholes for IV via vol_math.implied_vol.
#
# This is the only "fat" Databento query in the codebase: a one-year
# OPRA pull for a single underlying is on the order of tens of MB.
# Run it once per symbol per backfill (the iv_history table is the cache).

def get_historical_daily_options(symbol, start_date, end_date,
                                  dte_min=20, dte_max=40):
    """
    Returns list of joined contract-day records for `symbol`'s options that
    were `dte_min`..`dte_max` calendar days from expiry on each observation.

    Each record:
      {
        'date':   'YYYY-MM-DD' observation date (UTC date the bar settled),
        'expiry': 'YYYY-MM-DD',
        'strike': float,
        'type':   'call' | 'put',
        'close':  float (option close price),
        'volume': int,
        'dte':    int,
      }

    Returns [] when Databento is unavailable or the query yields nothing.
    """
    from datetime import date as _date  # local to avoid polluting module

    client = _get_client()
    if not client or _billing_blocked():
        return []

    if isinstance(start_date, datetime):
        start_date = start_date.date()
    if isinstance(end_date, datetime):
        end_date = end_date.date()

    parent    = symbol + ".OPT"
    start_iso = start_date.isoformat()
    end_iso   = end_date.isoformat()

    try:
        def_df = client.timeseries.get_range(
            dataset  = "OPRA.PILLAR",
            symbols  = [parent],
            stype_in = "parent",
            schema   = "definition",
            start    = start_iso,
            end      = end_iso,
        ).to_df()
    except Exception as e:
        if _is_billing_error(e):
            _trip_billing_breaker("{} definitions".format(symbol))
        else:
            _emit_error("databento.fetch_failed", source="definitions",
                        symbol=symbol, exc=type(e).__name__)
        return []

    if def_df is None or def_df.empty:
        return []

    # Build instrument_id -> (strike, expiry_date, type)
    inst_meta = {}
    for _, row in def_df.iterrows():
        try:
            iid = int(row.get("instrument_id"))
            strike = row.get("strike_price")
            if strike is None:
                continue
            strike = float(strike)
            if strike > 100000:           # OPRA quotes strikes in 1e9 fixed point
                strike = strike / 1e9

            expiry = row.get("expiration")
            if expiry is not None and hasattr(expiry, "date"):
                exp_date = expiry.date()
            elif expiry:
                exp_date = datetime.fromisoformat(str(expiry)[:10]).date()
            else:
                continue

            inst_class = str(row.get("instrument_class", "")).upper()
            opt_type = "call" if "C" in inst_class else \
                       "put"  if "P" in inst_class else None
            if not opt_type:
                continue
            inst_meta[iid] = (strike, exp_date, opt_type)
        except Exception:
            continue

    if not inst_meta:
        return []

    try:
        ohlcv_df = client.timeseries.get_range(
            dataset  = "OPRA.PILLAR",
            symbols  = [parent],
            stype_in = "parent",
            schema   = "ohlcv-1d",
            start    = start_iso,
            end      = end_iso,
        ).to_df()
    except Exception as e:
        if _is_billing_error(e):
            _trip_billing_breaker("{} ohlcv-1d".format(symbol))
        else:
            _emit_error("databento.fetch_failed", source="ohlcv-1d",
                        symbol=symbol, exc=type(e).__name__)
        return []

    if ohlcv_df is None or ohlcv_df.empty:
        return []

    out = []
    for _, row in ohlcv_df.iterrows():
        try:
            iid = int(row.get("instrument_id"))
            meta = inst_meta.get(iid)
            if not meta:
                continue
            strike, exp_date, opt_type = meta

            # ts_event is the bar's settlement timestamp (UTC).
            ts = row.get("ts_event")
            if ts is None:
                continue
            obs_date = ts.date() if hasattr(ts, "date") else \
                       datetime.fromisoformat(str(ts)[:10]).date()

            dte = (exp_date - obs_date).days
            if dte < dte_min or dte > dte_max:
                continue

            close = row.get("close")
            if close is None:
                continue
            close = float(close)
            if close > 1e6:               # OPRA prices use 1e9 fixed point too
                close = close / 1e9

            out.append({
                "date":   obs_date.isoformat(),
                "expiry": exp_date.isoformat(),
                "strike": strike,
                "type":   opt_type,
                "close":  close,
                "volume": int(row.get("volume", 0) or 0),
                "dte":    dte,
            })
        except Exception:
            continue

    return out


# =============================================
# LOCAL CACHE
# =============================================

_DB_CACHE = "databento_cache.db"


def _init_cache():
    conn = db_utils.connect(_DB_CACHE)
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
    conn = db_utils.connect(_DB_CACHE)
    c    = conn.cursor()
    c.execute("SELECT value, stored_at FROM cache WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    try:
        stored = datetime.fromisoformat(row[1])
        age    = (_utcnow() - stored).total_seconds()
        if age > max_age_seconds:
            return None
        return json.loads(row[0])
    except Exception:
        return None


def _cache_set(key, value):
    conn = db_utils.connect(_DB_CACHE)
    conn.execute("""
        INSERT OR REPLACE INTO cache (key, value, stored_at)
        VALUES (?, ?, ?)
    """, (key, json.dumps(value, default=str), _utcnow().isoformat()))
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
    DEPRECATED: Databento does not sell the VIX spot index.

    VX futures on XCBF.PITCH require a paid live license per their API:
        "A live data license is required to access XCBF.PITCH data
         after 2026-05-11T22:00:00Z"

    VIX index is now fetched via Yahoo Finance in regime_filter.py.
    This stub returns None so any legacy caller falls through to
    regime_filter's Yahoo path.
    """
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
    if not client or _billing_blocked():
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
        if _is_billing_error(e):
            _trip_billing_breaker("{} overnight".format(contract))
        else:
            _emit_error("databento.fetch_failed", source="overnight",
                        symbol=contract, exc=type(e).__name__)
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
    if not client or _billing_blocked():
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
        if _is_billing_error(e):
            _trip_billing_breaker("{} chain".format(underlying))
        else:
            _emit_error("databento.fetch_failed", source="chain",
                        symbol=underlying, exc=type(e).__name__)
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
    out["vix_note"] = ("VIX index is fetched via Yahoo Finance in regime_filter.py. "
                       "Databento doesn't sell the spot VIX value; XCBF.PITCH "
                       "futures require paid live license.")

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
