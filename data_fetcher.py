# data_fetcher.py
# Thin caching + parallel layer in front of the Alpaca bars/quote endpoints.
#
# Replaces the inline serial requests inside main.py's scan loop. Each series
# has its own TTL: intraday bars roll every 5 min (cache 30s), daily once per
# day (cache 1h), hourly bars roll on the hour (cache 30 min). Daily/hourly
# series previously refetched every 5-min scan even though the underlying data
# barely changes -- the cache cuts ~60% of HTTP volume.
#
# prefetch_symbols() runs all (symbol, series) pulls concurrently on a thread
# pool so a full scan of N symbols takes roughly max(latency) instead of
# N * 4 * latency.

import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytz
import requests
from cachetools import TTLCache


ALPACA_KEY    = os.getenv("APCA_API_KEY_ID", "").strip()
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY", "").strip()

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}

DATA_URL     = "https://data.alpaca.markets/v2/stocks/{}/bars"
QUOTE_URL    = "https://data.alpaca.markets/v2/stocks/{}/quotes/latest"
SNAPSHOT_URL = "https://data.alpaca.markets/v2/stocks/{}/snapshot"

# How old the freshest available price source is allowed to be (in seconds)
# during regular trading hours before we declare the symbol's price "stale"
# and refuse to signal on it. On free Alpaca (IEX-only feed), thinly-routed
# names like CRWV or AVGO during a fast move can return latestQuote prints
# that are minutes old -- this guard catches that.
STALE_PRICE_MAX_AGE_SECONDS = 60


_INTRADAY_CACHE = TTLCache(maxsize=256, ttl=30)
_DAILY_CACHE    = TTLCache(maxsize=256, ttl=3600)
_HR1_CACHE      = TTLCache(maxsize=256, ttl=1800)
_HR4_CACHE      = TTLCache(maxsize=256, ttl=3600)
_QUOTE_CACHE    = TTLCache(maxsize=256, ttl=5)
_LOCK           = threading.RLock()

_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="data-fetch")


_ET = pytz.timezone("America/New_York")


def _today_session_start_iso():
    """Today's RTH open in RFC3339 (Alpaca expects ISO 8601 with TZ)."""
    now_et = datetime.now(_ET)
    session_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    return session_open.isoformat()


def _fetch_bars(symbol, timeframe, limit, start=None):
    """
    Pull bars from Alpaca. When `start` is provided, returns only bars at
    or after that timestamp -- critical for intraday/ORB logic since
    Alpaca's default "latest N bars" silently bleeds yesterday's session
    in during the first hour of trading.
    """
    params = {"timeframe": timeframe, "limit": limit}
    if start:
        params["start"] = start
    try:
        r = requests.get(
            DATA_URL.format(symbol),
            headers=HEADERS,
            params=params,
            timeout=10,
        )
        if r.status_code != 200:
            return None
        return r.json().get("bars", [])
    except Exception as e:
        print("[data_fetcher] {} {} error: {}".format(symbol, timeframe, e))
        return None


def get_intraday(symbol):
    """
    Today's RTH 5-minute bars only. The `start` filter prevents Alpaca
    from returning yesterday's bars when fewer than 78 bars have printed
    today, which used to poison ORB/VWAP/gap calculations in the first
    hour of the session.
    """
    with _LOCK:
        cached = _INTRADAY_CACHE.get(symbol)
    if cached is not None:
        return cached
    bars = _fetch_bars(symbol, "5Min", 78, start=_today_session_start_iso())
    if bars is not None:
        with _LOCK:
            _INTRADAY_CACHE[symbol] = bars
    return bars


def get_daily(symbol):
    with _LOCK:
        cached = _DAILY_CACHE.get(symbol)
    if cached is not None:
        return cached
    bars = _fetch_bars(symbol, "1Day", 20)
    if bars is not None:
        with _LOCK:
            _DAILY_CACHE[symbol] = bars
    return bars


def get_1hr_bars(symbol):
    with _LOCK:
        cached = _HR1_CACHE.get(symbol)
    if cached is not None:
        return cached
    bars = _fetch_bars(symbol, "1Hour", 30)
    if bars is not None:
        with _LOCK:
            _HR1_CACHE[symbol] = bars
    return bars


def get_4hr_bars(symbol):
    with _LOCK:
        cached = _HR4_CACHE.get(symbol)
    if cached is not None:
        return cached
    bars = _fetch_bars(symbol, "1Hour", 80)
    if not bars:
        return None
    grouped = []
    for i in range(0, len(bars) - 3, 4):
        chunk = bars[i:i + 4]
        grouped.append({
            "o": chunk[0]["o"],
            "h": max(b["h"] for b in chunk),
            "l": min(b["l"] for b in chunk),
            "c": chunk[-1]["c"],
            "v": sum(b["v"] for b in chunk),
            "t": chunk[0]["t"],
        })
    with _LOCK:
        _HR4_CACHE[symbol] = grouped
    return grouped


def _parse_alpaca_ts(ts_str):
    """Alpaca timestamps look like '2026-05-14T13:41:02.123456789Z'.
    Trim sub-microsecond precision and parse to an aware UTC datetime."""
    if not ts_str:
        return None
    try:
        s = ts_str.replace("Z", "+00:00")
        # Truncate fractional seconds to 6 digits so fromisoformat accepts it
        if "." in s:
            head, tail = s.split(".", 1)
            frac, _, tz = tail.partition("+")
            if not tz:
                frac, _, tz = tail.partition("-")
                sep = "-"
            else:
                sep = "+"
            frac = frac[:6]
            s = "{}.{}{}{}".format(head, frac, sep, tz) if tz else "{}.{}".format(head, frac)
        return datetime.fromisoformat(s)
    except Exception:
        return None


def get_price_with_freshness(symbol):
    """
    Pull the freshest valid price for `symbol` from Alpaca's snapshot
    endpoint, which bundles latestTrade / latestQuote / minuteBar in a
    single request.

    Returns (price, age_seconds, source) -- or (None, None, None) if no
    usable price exists. Caller decides whether to act on the freshness.

    Priority (most reliable first):
      1. latestTrade.price     -- actual last execution
      2. minuteBar.close       -- close of the most recent 1-min bar
      3. (latestQuote.bp+ap)/2 -- top-of-book mid
    Sources are scored by their own timestamp; we return whichever is
    freshest with a sane price. Returning (price, age) lets us mark the
    symbol stale at the scanner level without losing the price entirely.
    """
    try:
        r = requests.get(SNAPSHOT_URL.format(symbol), headers=HEADERS, timeout=5)
        if r.status_code != 200:
            return None, None, None
        snap = r.json() or {}
    except Exception:
        return None, None, None

    now_utc = datetime.now(pytz.utc)

    candidates = []  # (age_seconds, price, source)

    trade = snap.get("latestTrade") or {}
    t_ts  = _parse_alpaca_ts(trade.get("t"))
    t_px  = trade.get("p")
    if t_ts and t_px and t_px > 0:
        candidates.append(((now_utc - t_ts).total_seconds(),
                           round(float(t_px), 2), "trade"))

    bar = snap.get("minuteBar") or {}
    b_ts = _parse_alpaca_ts(bar.get("t"))
    b_px = bar.get("c")
    if b_ts and b_px and b_px > 0:
        # Minute bars timestamp at bar OPEN; the bar's effective age is at
        # most ~60s newer than that. Subtract 30s so we don't penalize
        # fresh closing prints.
        age = max(0.0, (now_utc - b_ts).total_seconds() - 30.0)
        candidates.append((age, round(float(b_px), 2), "minuteBar"))

    quote = snap.get("latestQuote") or {}
    q_ts  = _parse_alpaca_ts(quote.get("t"))
    ap, bp = quote.get("ap"), quote.get("bp")
    if q_ts and ap and bp and ap > 0 and bp > 0:
        candidates.append(((now_utc - q_ts).total_seconds(),
                           round((float(ap) + float(bp)) / 2, 2), "quote"))

    if not candidates:
        return None, None, None

    candidates.sort(key=lambda x: x[0])
    age, price, source = candidates[0]
    return price, age, source


def get_current_price(symbol):
    """
    Backwards-compatible: returns the price or None.
    Caches for 5s. Internally calls get_price_with_freshness and discards
    the freshness/source info.
    """
    with _LOCK:
        cached = _QUOTE_CACHE.get(symbol)
    if cached is not None:
        return cached
    price, _age, _source = get_price_with_freshness(symbol)
    if price is not None:
        with _LOCK:
            _QUOTE_CACHE[symbol] = price
    return price


def is_price_stale(symbol):
    """
    Returns (is_stale, age_seconds, source). is_stale is True when the
    freshest price source for `symbol` exceeds STALE_PRICE_MAX_AGE_SECONDS
    during regular trading hours. Outside RTH we don't gate -- the scanner
    has its own pre-ORB / post-close logic. Used by the 0DTE scanner to
    skip signal generation on stocks whose IEX prints have gone stale.
    """
    now_et = datetime.now(_ET)
    et_minutes = now_et.hour * 60 + now_et.minute
    # RTH window 9:30 - 16:00 ET
    in_rth = (570 <= et_minutes < 960) and now_et.weekday() < 5
    price, age, source = get_price_with_freshness(symbol)
    if price is None or age is None:
        return True, None, None
    if in_rth and age > STALE_PRICE_MAX_AGE_SECONDS:
        return True, age, source
    return False, age, source


def prefetch_symbols(symbols, series=("intraday", "daily", "1hr", "4hr")):
    """Warm caches for all (symbol, series) combinations in parallel.

    Subsequent get_* calls in the same scan cycle hit the cache and return
    immediately. Returns {symbol: {series: bars_or_None}} for callers that
    want the data directly.
    """
    fn = {
        "intraday": get_intraday,
        "daily":    get_daily,
        "1hr":      get_1hr_bars,
        "4hr":      get_4hr_bars,
    }
    futures = {}
    for sym in symbols:
        for kind in series:
            if kind not in fn:
                continue
            futures[(sym, kind)] = _EXECUTOR.submit(fn[kind], sym)
    out = {s: {} for s in symbols}
    for (sym, kind), fut in futures.items():
        try:
            out[sym][kind] = fut.result(timeout=15)
        except Exception:
            out[sym][kind] = None
    return out


def cache_stats():
    with _LOCK:
        return {
            "intraday": (_INTRADAY_CACHE.currsize, _INTRADAY_CACHE.maxsize),
            "daily":    (_DAILY_CACHE.currsize,    _DAILY_CACHE.maxsize),
            "1hr":      (_HR1_CACHE.currsize,      _HR1_CACHE.maxsize),
            "4hr":      (_HR4_CACHE.currsize,      _HR4_CACHE.maxsize),
            "quote":    (_QUOTE_CACHE.currsize,    _QUOTE_CACHE.maxsize),
        }


def clear_caches():
    with _LOCK:
        for c in (_INTRADAY_CACHE, _DAILY_CACHE, _HR1_CACHE, _HR4_CACHE, _QUOTE_CACHE):
            c.clear()
