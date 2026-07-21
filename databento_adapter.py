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
import re
import sys
import json
import time
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
    """Hard account-state errors that mean "stop calling Databento entirely".

    Two cases, both of which must trip the cooldown breaker rather than be
    retried per-symbol:
      * 402 account_insufficient_funds -- out of credit.
      * 403 auth_account_locked -- account locked/suspended (e.g. a failed
        payment). Databento charges for every request *even when rejected*,
        so re-hitting a locked account across 72 symbols on every scan both
        burns money and floods the logs with 403s. Classify it here so the
        breaker opens on the first occurrence.

    Window/licensing rejections (422 data_end_after_available_end, 403
    license_not_found_unauthorized) are NOT account-state errors: they mean
    "this query's end reaches into a period this account can't read", the
    account itself is fine, and clamping the window fixes the query. They
    used to match the blanket 403 test below and open the shared breaker,
    blanking OI/GEX/IV for 30 minutes over a single mis-sized window.
    """
    if _is_window_error(exc):
        return False
    s = "{} {}".format(type(exc).__name__, exc).lower()
    return (
        "account_insufficient_funds" in s
        or "insufficient budget" in s
        or "insufficient funds" in s
        or " 402" in s
        or s.startswith("402")
        # Account locked / suspended / auth rejected -> stop calling.
        or "auth_account_locked" in s
        or "account_locked" in s
        or "account has been locked" in s
        or "locked for security" in s
        or " 403" in s
        or s.startswith("403")
    )


def _billing_blocked():
    return (
        _billing_blocked_until is not None
        and _utcnow() < _billing_blocked_until
    )


# Transient server/network errors (gateway timeouts, 5xx, dropped
# connections) are NOT billing problems and must not trip the 30-minute
# billing breaker -- doing so took down the whole GEX snapshot for half an
# hour over a single 504. We retry these a couple of times with backoff and,
# if they persist, log a fetch_failed and move on WITHOUT opening the breaker.
_TRANSIENT_MARKERS = (
    " 500", " 502", " 503", " 504", " 408", " 429",
    "timed out", "timeout", "gateway", "temporarily unavailable",
    "connection reset", "connection aborted", "read timed out",
    "remote end closed", "service unavailable",
)


def _is_transient_error(exc):
    if _is_billing_error(exc):
        return False
    s = "{} {}".format(type(exc).__name__, exc).lower()
    return any(m in s for m in _TRANSIENT_MARKERS)


# =============================================
# AVAILABILITY / LICENSING HORIZON
# =============================================
#
# The historical API rejects windows that reach past what the account can
# read, in two flavors:
#   * 422 data_end_after_available_end -- `end` is beyond the dataset's
#     published range (e.g. asking EQUS.MINI for "tomorrow" to capture
#     today's bar before today has settled).
#   * 403 license_not_found_unauthorized -- the window reaches into the
#     recent period (roughly the current/last session) that Databento only
#     serves to accounts holding a LIVE data license for that dataset
#     ("A live data license is required to access OPRA.PILLAR data after
#     <cutoff>").
# Both messages carry the exact cutoff timestamp, so instead of failing the
# fetch (and, for the 403, tripping the shared billing breaker), we parse
# the cutoff, remember it per dataset, clamp `end` to it and retry once.
# Later calls clamp preemptively while the learned cutoff is fresh, so
# steady state doesn't pay for a rejected probe first.

_WINDOW_ERROR_MARKERS = (
    "data_end_after_available_end",
    "data_start_after_available_end",
    "license_not_found",
    "live data license",
)

# "has data available up to '2026-07-21 00:00:00+00:00'"  (422)
# "access OPRA.PILLAR data after 2026-07-20T13:30:00.000000000Z"  (403)
_CUTOFF_RES = (
    re.compile(r"available up to '([^']+)'"),
    re.compile(r"data after\s+(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"),
)

_HORIZON_TTL_SECS = 4 * 3600
_avail_horizon = {}   # dataset -> {"end": naive-UTC datetime, "learned_at": float}


def _is_window_error(exc):
    s = "{} {}".format(type(exc).__name__, exc).lower()
    return any(m in s for m in _WINDOW_ERROR_MARKERS)


def _parse_when(value):
    """'YYYY-MM-DD[ T]HH:MM:SS...' or bare 'YYYY-MM-DD' -> naive-UTC
    datetime (bare dates are midnight UTC, matching Databento semantics).
    Returns None when unparseable."""
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T")[:19])
    except (ValueError, TypeError):
        return None


def _availability_cutoff(exc):
    """Extract the server-reported cutoff from a window/licensing rejection.
    Returns a naive-UTC datetime, or None when the message carries none."""
    s = str(exc)
    for rx in _CUTOFF_RES:
        m = rx.search(s)
        if m:
            return _parse_when(m.group(1))
    return None


def _learn_horizon(dataset, cutoff):
    _avail_horizon[dataset] = {"end": cutoff, "learned_at": time.time()}


def _horizon_end(dataset):
    h = _avail_horizon.get(dataset)
    if not h or time.time() - h["learned_at"] > _HORIZON_TTL_SECS:
        return None
    return h["end"]


def _clamp_end(dataset, end):
    """Clamp a get_range `end` to the dataset's learned horizon. Returns the
    (possibly unchanged) end as an ISO string usable in the API call."""
    horizon = _horizon_end(dataset)
    if horizon is None:
        return end
    end_dt = _parse_when(end)
    if end_dt is None or end_dt <= horizon:
        return end
    return horizon.strftime("%Y-%m-%dT%H:%M:%S")


def _get_range_clamped(client, dataset, start, end, **kwargs):
    """timeseries.get_range with window/licensing-horizon handling.

    Preemptively clamps `end` to the learned horizon for `dataset`; when the
    server still rejects the window, learns the reported cutoff and retries
    once with the clamped end. Returns a DataFrame, or None when the clamped
    window is empty (start >= usable end -- the whole window sits in the
    live-license-only period). Errors that aren't window/licensing problems
    propagate to the caller unchanged.
    """
    def _pull(end_value):
        return _pull_with_retry(lambda: client.timeseries.get_range(
            dataset=dataset, start=start, end=end_value, **kwargs
        ).to_df())

    start_dt = _parse_when(start)

    def _empty(end_value):
        end_dt = _parse_when(end_value)
        return (start_dt is not None and end_dt is not None
                and end_dt <= start_dt)

    first_end = _clamp_end(dataset, end)
    if _empty(first_end):
        return None
    try:
        return _pull(first_end)
    except Exception as e:
        cutoff = _availability_cutoff(e) if _is_window_error(e) else None
        if cutoff is None:
            raise
        _learn_horizon(dataset, cutoff)
        retry_end = _clamp_end(dataset, end)
        if retry_end == first_end:
            raise
        if _empty(retry_end):
            return None
        return _pull(retry_end)


def _pull_with_retry(fn, retries=2, backoff=1.5):
    """
    Run a Databento timeseries/symbology call, retrying transient failures.

    Billing (402) errors propagate immediately so the caller can trip the
    breaker. Non-transient, non-billing errors also propagate on the first
    occurrence. Transient errors are retried up to `retries` times with
    exponential backoff before the final exception propagates.
    """
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as e:
            if attempt < retries and _is_transient_error(e):
                time.sleep(backoff)
                backoff *= 2
                attempt += 1
                continue
            raise


_LOG_JSON = os.getenv("LOG_JSON", "").strip() in ("1", "true", "yes")

# Optional sink registered by main so adapter-level errors (most importantly
# databento.billing_blocked) also reach the in-app debug-log ring buffer.
# Without it the breaker's root-cause line lives only on stderr, while the
# debug log shows nothing but downstream symptoms (gex.snapshot_empty,
# "OI snapshots: 0/72").
_log_mirror = None


def register_log_mirror(callback):
    """Register a `callback(line: str)` receiving each _emit_error line,
    pre-formatted. Exceptions from the callback are swallowed."""
    global _log_mirror
    _log_mirror = callback


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
    if _log_mirror is not None:
        try:
            _log_mirror(line)
        except Exception:
            pass


# Last breaker trip, kept for diagnostics: the breaker itself only stores
# "blocked until", so once tripped there was no way to see WHY from the
# /databento or /diag endpoints — the root cause lived in a stderr line that
# rotates out of the debug-log ring. In-memory only; a redeploy clears it.
_last_billing_trip = None   # {"at", "context", "detail"} or None


def _trip_billing_breaker(context, exc=None):
    global _billing_blocked_until, _last_billing_trip
    first_trip = not _billing_blocked()
    _billing_blocked_until = _utcnow() + timedelta(
        seconds=_BILLING_COOLDOWN_SECS
    )
    _last_billing_trip = {
        "at":      _utcnow().isoformat() + "Z",
        "context": context,
        "detail":  str(exc)[:300] if exc is not None else None,
    }
    if first_trip:
        _emit_error(
            "databento.billing_blocked",
            context=context,
            cooldown_min=_BILLING_COOLDOWN_SECS // 60,
            until=_billing_blocked_until.isoformat() + "Z",
            detail=(str(exc)[:200] if exc is not None else ""),
        )


def billing_status():
    """Returns dict with breaker state for the /databento and /diag
    endpoints, including why the breaker last opened (this process only)."""
    return {
        "blocked":       _billing_blocked(),
        "blocked_until": (_billing_blocked_until.isoformat() + "Z")
                         if _billing_blocked_until else None,
        "last_trip":     _last_billing_trip,
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
        def_df = _get_range_clamped(
            client, "OPRA.PILLAR", start_iso, end_iso,
            symbols  = [parent],
            stype_in = "parent",
            schema   = "definition",
        )
    except Exception as e:
        if _is_billing_error(e):
            _trip_billing_breaker("{} definitions".format(symbol), e)
        else:
            _emit_error("databento.fetch_failed", source="definitions",
                        symbol=symbol, exc=type(e).__name__,
                        detail=str(e)[:300])
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
        ohlcv_df = _get_range_clamped(
            client, "OPRA.PILLAR", start_iso, end_iso,
            symbols  = [parent],
            stype_in = "parent",
            schema   = "ohlcv-1d",
        )
    except Exception as e:
        if _is_billing_error(e):
            _trip_billing_breaker("{} ohlcv-1d".format(symbol), e)
        else:
            _emit_error("databento.fetch_failed", source="ohlcv-1d",
                        symbol=symbol, exc=type(e).__name__,
                        detail=str(e)[:300])
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

# Persist on the Railway volume. As a bare relative path this lived in
# the container's ephemeral dir, so every redeploy wiped the 1-hour
# chain cache -- defeating the cost guard and forcing fresh OPRA pulls.
_DB_CACHE = ((os.getenv("DATA_DIR")
              or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
              or "/tmp").rstrip("/") + "/databento_cache.db")


# Cache keys are date-stamped (chain_SPY_2026-06-12, on_ES_..., neg_...), so
# entries are never overwritten by newer days -- without a sweep the table
# grows by a few full-chain JSON blobs per day, forever, on the persistent
# volume. Nothing reads entries past its max_age (longest is 1h; neg 20min),
# so anything older than this is dead weight.
_CACHE_RETENTION_SECS = 3 * 24 * 3600
_last_cache_prune     = 0.0


def _prune_cache(conn):
    cutoff = (_utcnow() - timedelta(seconds=_CACHE_RETENTION_SECS)).isoformat()
    conn.execute("DELETE FROM cache WHERE stored_at < ?", (cutoff,))


def _init_cache():
    conn = db_utils.connect(_DB_CACHE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            key       TEXT PRIMARY KEY,
            value     TEXT,
            stored_at TEXT
        )
    """)
    _prune_cache(conn)
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
    global _last_cache_prune
    conn = db_utils.connect(_DB_CACHE)
    conn.execute("""
        INSERT OR REPLACE INTO cache (key, value, stored_at)
        VALUES (?, ?, ?)
    """, (key, json.dumps(value, default=str), _utcnow().isoformat()))
    # Startup prune covers redeploys; this covers long-lived processes.
    if time.time() - _last_cache_prune > 24 * 3600:
        _last_cache_prune = time.time()
        _prune_cache(conn)
    conn.commit()
    conn.close()


# ---- Negative-result cache --------------------------------------------------
# Databento bills for EVERY request, including ones that come back empty or are
# rejected. The positive cache only stores successful payloads, so a query that
# returned nothing (OPRA batch not published yet, a name with no listed options,
# a transient failure) used to be re-issued -- and re-charged -- on every
# scheduler tick and by every caller. Remember "this query just came back empty"
# for a short window so we don't pay for the same miss repeatedly. The TTL is
# kept below the 30-minute EOD sweep retry gap so a legitimately-delayed batch
# still recovers on the next scheduled retry.
_NEG_CACHE_TTL = 20 * 60


def _neg_cached(key):
    """True if `key` recently returned empty/failed -- skip the paid re-pull."""
    return _cache_get("neg_" + key, max_age_seconds=_NEG_CACHE_TTL) is not None


def _neg_cache_set(key):
    _cache_set("neg_" + key, {"empty": True})


# =============================================
# CONSOLIDATED US EQUITY OHLCV BARS
# =============================================
#
# Primary daily/weekly bar source for the swing engine and the regime read.
# Databento's consolidated US-equities feed is far cleaner than the free
# Alpaca/IEX daily bars, which intermittently print bad ticks (zeros,
# mis-decimaled closes) that silently corrupt relative strength, the swing
# detectors, and every level/target derived from the series. Alpaca remains
# the fallback when Databento is unavailable.
#
# Dataset is env-configurable because account entitlements vary. Default is
# EQUS.MINI (Databento "US Equities Mini" -- the consolidated 15-exchange +
# 30-ATS feed). Set DATABENTO_EQUITY_DATASET to override (e.g. EQUS.SUMMARY,
# DBEQ.BASIC, or a single-venue feed like XNAS.ITCH).

EQUITY_DATASET = os.getenv("DATABENTO_EQUITY_DATASET", "EQUS.MINI").strip()


def _is_insufficient_funds(exc):
    """Narrow 402-only check. Unlike _is_billing_error this does NOT treat a
    403 as billing: a 403 on the equity dataset usually means "not entitled to
    this dataset", which must fall back to Alpaca rather than open the shared
    breaker and take down the funded options/futures paths."""
    s = "{} {}".format(type(exc).__name__, exc).lower()
    return (
        "account_insufficient_funds" in s
        or "insufficient budget" in s
        or "insufficient funds" in s
        or " 402" in s
        or s.startswith("402")
    )


def get_equity_bars(symbol, start, end, schema="ohlcv-1d"):
    """Consolidated US-equity OHLCV bars from Databento.

    Returns an ascending list of {t,o,h,l,c,v} dicts, or [] when Databento is
    unavailable, the breaker is open, or the query yields nothing. `start`/`end`
    are 'YYYY-MM-DD' strings; `end` is exclusive, so pass tomorrow to include
    today's (settled) bar.

    Only a genuine 402 insufficient-funds trips the shared billing breaker;
    entitlement/symbology/availability errors are equity-local -- they
    negative-cache and let the caller fall back to Alpaca.
    """
    client = _get_client()
    if not client or _billing_blocked():
        return []

    cache_key = "eqbars_{}_{}_{}_{}".format(symbol, schema, start, end)
    if _neg_cached(cache_key):
        return []

    try:
        df = _get_range_clamped(
            client, EQUITY_DATASET, start, end,
            symbols  = [symbol],
            stype_in = "raw_symbol",
            schema   = schema,
        )
    except Exception as e:
        if _is_insufficient_funds(e):
            _trip_billing_breaker("equity bars", e)
        else:
            # Surface the actual Databento detail, not just the class name.
            # `BentoClientError` alone is undiagnosable -- the SDK carries the
            # real reason (bad dataset code, no entitlement, symbology
            # mismatch, out-of-range window) in the message, and without it
            # the log can't tell us why every symbol fails identically.
            _emit_error("databento.equity_fetch_failed", symbol=symbol,
                        dataset=EQUITY_DATASET, exc=type(e).__name__,
                        detail=str(e)[:300])
            _neg_cache_set(cache_key)
        return []

    if df is None or df.empty:
        _neg_cache_set(cache_key)
        return []

    bars = []
    for ts, row in df.iterrows():
        try:
            bars.append({
                "t": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
                "v": int(row.get("volume", 0) or 0),
            })
        except Exception:
            continue
    return bars


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

def get_vix_proxy(target_date_et=None):
    """
    Spot-VIX estimate from VX futures, term-structure adjusted.

    Front-month VX trades above spot VIX in contango (typically ~1-2
    points, occasionally 3+ near the start of a roll cycle). Pulling
    just VX.c.0 understated spot by the full basis. Pulling the next
    contract too lets us approximate the slope and back out a spot
    estimate that lands within ~1 point of the real index most days:

        spot ≈ VX.c.0 - 0.5 * (VX.c.1 - VX.c.0)

    The 0.5 coefficient is the empirical average of the front contract's
    time-to-expiry over its 30-day life (it sits "halfway" up the curve
    on average). Not perfect in fast curve moves, but far closer than
    raw front-month and the bucketing for regime stays stable.
    """
    client = _get_client()
    if not client or _billing_blocked():
        return None

    et = pytz.timezone("America/New_York")
    if target_date_et is None:
        target_date_et = datetime.now(et).date()

    cache_key = "vixproxy_{}".format(target_date_et.isoformat())
    cached = _cache_get(cache_key, max_age_seconds=3600)
    if cached:
        return cached.get("vix")
    if _neg_cached(cache_key):
        return None

    # 7-day window so weekends/holidays still yield a recent settlement.
    start_iso = (target_date_et - timedelta(days=7)).isoformat()
    end_iso   = target_date_et.isoformat()
    try:
        df = _get_range_clamped(
            client, "XCBF.PITCH", start_iso, end_iso,
            symbols  = ["VX.c.0", "VX.c.1"],
            stype_in = "continuous",
            schema   = "ohlcv-1d",
        )
        if df is None or df.empty:
            print("[databento] VX proxy empty. Window: {} -> {}".format(
                start_iso, end_iso))
            _neg_cache_set(cache_key)
            return None

        # Extract latest close per contract. Databento puts the resolved
        # symbol in a 'symbol' column when more than one is requested.
        front = second = None
        if "symbol" in df.columns:
            f_rows = df[df["symbol"] == "VX.c.0"]
            s_rows = df[df["symbol"] == "VX.c.1"]
            if not f_rows.empty:
                front = float(f_rows["close"].iloc[-1])
            if not s_rows.empty:
                second = float(s_rows["close"].iloc[-1])
        else:
            front = float(df["close"].iloc[-1])

        if front is None:
            print("[databento] VX proxy: no front-month data")
            return None

        if second is not None:
            spot_est = front - 0.5 * (second - front)
            print("[databento] VX term-structure: front={:.2f} 2nd={:.2f} "
                  "-> spot_est={:.2f}".format(front, second, spot_est))
        else:
            # Fall back to raw front if 2nd-month isn't available.
            spot_est = front
            print("[databento] VX proxy: 2nd-month unavailable, "
                  "returning raw front={:.2f}".format(front))

        if not (5.0 <= spot_est <= 90.0):
            print("[databento] VX-derived VIX out of range: {}".format(spot_est))
            return None
        vix = round(float(spot_est), 2)
        _cache_set(cache_key, {"vix": vix})
        return vix
    except Exception as e:
        # XCBF.PITCH (VX futures) requires a paid live-data license and
        # returns 403 without one. Classifying that 403 as "billing" (via the
        # broad _is_billing_error) trips the SHARED breaker and blanks the
        # funded equity + options paths for 30 min over a single non-critical
        # proxy -- exactly the "databento.billing_blocked | context=vix proxy"
        # cascade seen at the open. Only a genuine 402 insufficient-funds may
        # open the breaker here; a 403/entitlement error negative-caches and
        # falls through to the Yahoo/VIXY VIX sources instead.
        if _is_insufficient_funds(e):
            _trip_billing_breaker("vix proxy", e)
        else:
            _emit_error("databento.fetch_failed", source="vix_proxy",
                        exc=type(e).__name__, detail=str(e)[:300])
            _neg_cache_set(cache_key)
        return None


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
    if _neg_cached(cache_key):
        return []

    symbol = CONTRACT_MAP.get(contract, "ES.n.0")
    try:
        df = _get_range_clamped(
            client, "GLBX.MDP3", start_iso, end_iso,
            symbols  = [symbol],
            stype_in = "continuous",
            schema   = "ohlcv-1h",
        )

        if df is None or df.empty:
            print("[databento] {} overnight empty. Window: {} -> {}".format(
                contract, start_iso, end_iso))
            _neg_cache_set(cache_key)
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
            _trip_billing_breaker("{} overnight".format(contract), e)
        else:
            _emit_error("databento.fetch_failed", source="overnight",
                        symbol=contract, exc=type(e).__name__,
                        detail=str(e)[:300])
            _neg_cache_set(cache_key)
        return []


# =============================================
# OPTIONS CHAIN with OI (for GEX)
# =============================================

def _parse_osi_symbol(raw):
    """
    Parse an OSI/OCC option raw_symbol into (strike, expiry, type).

    OSI layout: 6-char root (space-padded), YYMMDD, C|P, 8-digit strike
    in 1/1000ths. Databento emits either the padded form ("SPY   240315
    C00450000") or compact ("SPY240315C00450000") -- we strip whitespace
    and parse the 15-char tail (YYMMDD + C/P + strike).
    """
    s = str(raw).strip().replace(" ", "")
    if len(s) < 15:
        return None
    tail = s[-15:]
    try:
        year   = 2000 + int(tail[0:2])
        month  = int(tail[2:4])
        day    = int(tail[4:6])
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        cp     = tail[6].upper()
        if   cp == "C": opt_type = "call"
        elif cp == "P": opt_type = "put"
        else: return None
        strike = int(tail[7:15]) / 1000.0
        return {
            "strike": strike,
            "expiry": "{:04d}-{:02d}-{:02d}".format(year, month, day),
            "type":   opt_type,
        }
    except (ValueError, TypeError):
        return None


# Databento statistics stat_type values (from databento/dbn enums.rs):
#   3=SettlementPrice 9=OpenInterest 11=ClosePrice 20=IndicativeClosePrice
#   10=FixingPrice 1=OpeningPrice. For a daily snapshot we want OI plus a
# representative daily price to back out IV. Prefer the official settlement,
# then the close, then progressively weaker fallbacks.
_STAT_OPEN_INTEREST   = 9
_STAT_PRICE_PRIORITY  = {3: 5, 11: 4, 20: 3, 10: 2, 1: 1}  # higher = preferred


def _scale_dbn_price(raw):
    """Normalize a Databento price to dollars.

    `to_df()` usually returns float dollars, but some SDK/schema paths still
    surface raw 1e-9 fixed-point integers (the existing OHLCV/strike code
    guards the same way). The UNDEF sentinel (i64 max ~9.22e18, or ~9.22e9
    after scaling) and non-positive values are rejected.
    """
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v > 1e6:            # fixed-point nanos (no option trades at $1M+)
        v = v / 1e9
    if not (0 < v < 1e5):  # still absurd -> UNDEF/garbage
        return None
    return v


def get_options_chain_snapshot(underlying, target_date_et=None,
                                 expiries_ahead=3, with_price=False,
                                 roots=None):
    """
    Returns EOD options chain with open interest (and, when with_price=True,
    a per-contract daily price used downstream to solve implied volatility).

    roots: optional list of OPRA option roots to pull as parents in ONE
    request. Cash-index options split across roots (SPX monthly AM-settled +
    SPXW daily PM-settled; NDX + NDXP), and OPRA parent symbology groups by
    root, so pulling just "SPX.OPT" would miss every weekday expiry. Pass
    roots=["SPX", "SPXW"] to get the full chain. Defaults to [underlying].

    Cost-optimized: pulls only the `statistics` schema (1d window, with a
    4d fallback for long weekends) for the parent symbol, then resolves
    instrument_id -> raw_symbol via `symbology.resolve` (a reference lookup,
    not a metered timeseries pull). Strike/expiry/type are parsed from the
    OSI tail. Open interest comes from stat_type=9; a representative price
    from the price stat_types (settlement/close/...) in the SAME pull, so
    enabling prices for GEX is free.

    with_price: also extract per-contract price and, only if the statistics
    schema carried none, do a single cheap `ohlcv-1d` parent pull for close
    prices. Gated because the 72-symbol OI sweep doesn't need prices and
    shouldn't pay for the fallback. GEX (SPY/QQQ only) passes True.

    The prior implementation pulled a 60-180 day `definition` delta stream
    for every snapshot, which on SPY/QQQ runs to tens of dollars per
    request and was the cause of the 402 / billing-breaker trips.
    """
    client = _get_client()
    if not client or _billing_blocked():
        return []

    et = pytz.timezone("America/New_York")
    if target_date_et is None:
        target_date_et = datetime.now(et).date()

    # Multi-root pulls (index options) get their own cache identity so they
    # can't be satisfied by — or clobber — a single-root pull for the same
    # underlying. Single-root keys keep the legacy format.
    root_list = list(roots) if roots else [underlying]
    key_sym   = "+".join(root_list) if roots else underlying

    # Price-enriched chains cache separately so a price-less OI-sweep result
    # can't satisfy a GEX request (which needs the price to compute IV).
    cache_key = "chain_{}{}_{}".format(
        "px_" if with_price else "", key_sym, target_date_et.isoformat())
    cached = _cache_get(cache_key, max_age_seconds=3600)
    if cached:
        return cached.get("chain", [])
    # A price-enriched chain (the GEX path) is a superset of the OI-only
    # chain: same statistics pull, plus per-contract prices OI consumers
    # ignore. Serve it to price-less requests instead of re-paying for the
    # identical pull under the other cache key.
    if not with_price:
        cached = _cache_get("chain_px_{}_{}".format(
            key_sym, target_date_et.isoformat()), max_age_seconds=3600)
        if cached:
            return cached.get("chain", [])
    # Don't re-pay for a query that just came back empty/failed.
    if _neg_cached(cache_key):
        return []

    end_iso = target_date_et.isoformat()
    parents = [r + ".OPT" for r in root_list]

    def _pull_stats(start):
        return _get_range_clamped(
            client, "OPRA.PILLAR", start, end_iso,
            symbols  = parents,
            stype_in = "parent",
            schema   = "statistics",
        )

    # 1) Statistics: stat_type=9 (OpenInterest) is published daily per
    #    active contract, so a 1d window captures the full chain. Widen
    #    to 4d only if the first pull is empty (long weekend / holiday).
    #    Transient 5xx/timeouts retry; only genuine 402s trip the breaker.
    #
    #    WINDOW SEMANTICS (why the sweeps run in the MORNING): get_range's
    #    `end` is EXCLUSIVE and bare dates resolve to midnight UTC, so with
    #    target=today this window is [today-1d 00:00Z, today 00:00Z) — it
    #    can only return records disseminated on the UTC day BEFORE today.
    #    An evening run therefore fetches the day-old batch (yesterday's
    #    settlements, OI as of two closes ago). A morning run lands the
    #    same window exactly on yesterday's full batch: yesterday's
    #    settlements + the OI print reflecting positions after the prior
    #    close — one session fresher on both, and the freshest thing the
    #    historical API reliably has premarket.
    try:
        stats_start = (target_date_et - timedelta(days=1)).isoformat()
        stats_df = _pull_stats(stats_start)
        if stats_df is None or stats_df.empty:
            stats_start = (target_date_et - timedelta(days=4)).isoformat()
            stats_df = _pull_stats(stats_start)
    except Exception as e:
        if _is_billing_error(e):
            _trip_billing_breaker("{} statistics".format(underlying), e)
        else:
            _emit_error("databento.fetch_failed", source="statistics",
                        symbol=underlying, exc=type(e).__name__,
                        msg=str(e)[:200])
            _neg_cache_set(cache_key)
        return []

    if stats_df is None or stats_df.empty:
        print("[databento] {} statistics empty".format(underlying))
        _neg_cache_set(cache_key)
        return []

    has_stat_type = "stat_type" in stats_df.columns
    if has_stat_type:
        oi_df = stats_df[stats_df["stat_type"] == _STAT_OPEN_INTEREST]
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

    if not oi_by_inst:
        print("[databento] {} no OI rows (stats_total={} stat9_rows={})".format(
            underlying, len(stats_df),
            len(oi_df) if oi_df is not None else 0))
        _neg_cache_set(cache_key)
        return []

    # 1b) Per-contract daily price from the SAME statistics pull (free).
    #     Keep the highest-priority price stat seen per instrument.
    px_by_inst = {}
    vol_by_inst = {}   # per-contract daily option volume (from ohlcv-1d pull)
    if with_price and has_stat_type:
        px_df = stats_df[stats_df["stat_type"].isin(_STAT_PRICE_PRIORITY.keys())]
        best_rank = {}
        for _, row in px_df.iterrows():
            try:
                iid  = int(row.get("instrument_id"))
                rank = _STAT_PRICE_PRIORITY.get(int(row.get("stat_type")), 0)
                if rank <= best_rank.get(iid, 0):
                    continue
                px = _scale_dbn_price(row.get("price"))
                if px is None:
                    continue
                px_by_inst[iid] = px
                best_rank[iid]  = rank
            except Exception:
                continue

    # 2) Resolve instrument_id -> raw_symbol. Symbology lookups are
    #    metadata calls (not metered as data records), so this is the
    #    cheap part. Chunked to stay under the 2000-symbol-per-request
    #    cap on the resolve endpoint.
    iids = list(oi_by_inst.keys())
    raw_by_iid = {}
    CHUNK = 2000

    def _resolve(batch, on_date):
        return _pull_with_retry(lambda: client.symbology.resolve(
            dataset    = "OPRA.PILLAR",
            symbols    = [str(x) for x in batch],
            stype_in   = "instrument_id",
            stype_out  = "raw_symbol",
            start_date = on_date,
        ))

    # resolve's implied range is [start_date, start_date + 1d), so a
    # start_date of "today" reaches into the live-license-only period and
    # 403s (license_not_found_unauthorized) even though the OI stats we're
    # resolving were disseminated a day earlier. Back the date off to a day
    # the license horizon fully covers; option instrument_id -> OSI symbol
    # mappings are stable day-over-day, so a day-old resolve is equivalent.
    resolve_date = end_iso
    horizon = _horizon_end("OPRA.PILLAR")
    if horizon is not None:
        resolve_date = min(resolve_date,
                           (horizon - timedelta(days=1)).date().isoformat())
    try:
        for i in range(0, len(iids), CHUNK):
            batch = iids[i:i + CHUNK]
            try:
                resp = _resolve(batch, resolve_date)
            except Exception as e:
                cutoff = _availability_cutoff(e) if _is_window_error(e) else None
                if cutoff is None:
                    raise
                _learn_horizon("OPRA.PILLAR", cutoff)
                resolve_date = (cutoff - timedelta(days=1)).date().isoformat()
                resp = _resolve(batch, resolve_date)
            result = (resp or {}).get("result") or {}
            for iid_str, mappings in result.items():
                if not mappings:
                    continue
                raw = mappings[-1].get("s")
                if not raw:
                    continue
                try:
                    raw_by_iid[int(iid_str)] = raw
                except Exception:
                    continue
    except Exception as e:
        if _is_billing_error(e):
            _trip_billing_breaker("{} resolve".format(underlying), e)
        else:
            _emit_error("databento.fetch_failed", source="resolve",
                        symbol=underlying, exc=type(e).__name__,
                        msg=str(e)[:200])
            _neg_cache_set(cache_key)
        return []

    # 2b) Price fallback: if the statistics schema carried no usable price
    #     stats, do ONE cheap ohlcv-1d parent pull for daily closes. Only on
    #     the with_price (GEX) path so the OI sweep stays free.
    if with_price and not px_by_inst:
        try:
            ohlcv_df = _get_range_clamped(
                client, "OPRA.PILLAR",
                (target_date_et - timedelta(days=1)).isoformat(), end_iso,
                symbols  = parents,
                stype_in = "parent",
                schema   = "ohlcv-1d",
            )
            if ohlcv_df is not None and not ohlcv_df.empty:
                # Latest close + volume per instrument (df is time-ordered).
                for _, row in ohlcv_df.iterrows():
                    try:
                        iid = int(row.get("instrument_id"))
                        px  = _scale_dbn_price(row.get("close"))
                        if px is not None:
                            px_by_inst[iid] = px
                        vol = row.get("volume")
                        if vol is not None:
                            vol_by_inst[iid] = int(vol)
                    except Exception:
                        continue
        except Exception as e:
            if _is_billing_error(e):
                _trip_billing_breaker("{} ohlcv-1d".format(underlying), e)
                return []
            _emit_error("databento.fetch_failed", source="ohlcv-1d-px",
                        symbol=underlying, exc=type(e).__name__,
                        msg=str(e)[:200])
            # Non-fatal: continue with OI-only chain (IV solved downstream
            # will just be skipped for contracts without a price).

    # 3) Build chain rows from OSI symbol parses.
    chain = []
    for iid, oi in oi_by_inst.items():
        raw = raw_by_iid.get(iid)
        if not raw:
            continue
        parsed = _parse_osi_symbol(raw)
        if not parsed:
            continue
        chain.append({
            "strike":              parsed["strike"],
            "expiry":              parsed["expiry"],
            "type":                parsed["type"],
            "open_interest":       oi,
            "volume":              vol_by_inst.get(iid),
            "price":               px_by_inst.get(iid),
            "implied_volatility":  None,
        })

    if chain:
        chain.sort(key=lambda x: x["expiry"])
        unique_exp = sorted(set(c["expiry"] for c in chain))
        keep = set(unique_exp[:expiries_ahead])
        chain = [c for c in chain if c["expiry"] in keep]
    else:
        sample_raw = next(iter(raw_by_iid.values()), None)
        print("[databento] {} chain empty: stats_total={} oi_iids={} "
              "resolved={} sample_raw={!r}".format(
                  underlying, len(stats_df), len(oi_by_inst),
                  len(raw_by_iid), sample_raw))

    _cache_set(cache_key, {"chain": chain})
    return chain


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
        "billing":       billing_status(),
    }
    if not is_available():
        if not _SDK_AVAILABLE:
            out["note"] = "Install databento SDK: pip install databento"
        elif not DATABENTO_KEY:
            out["note"] = "Set DATABENTO_API_KEY env var."
        elif _billing_blocked():
            out["note"] = ("Billing breaker open — see billing.last_trip "
                           "for the failing call and error detail.")
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
