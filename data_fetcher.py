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

import requests
from cachetools import TTLCache


ALPACA_KEY    = os.getenv("APCA_API_KEY_ID", "").strip()
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY", "").strip()

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}

DATA_URL  = "https://data.alpaca.markets/v2/stocks/{}/bars"
QUOTE_URL = "https://data.alpaca.markets/v2/stocks/{}/quotes/latest"


_INTRADAY_CACHE = TTLCache(maxsize=256, ttl=30)
_DAILY_CACHE    = TTLCache(maxsize=256, ttl=3600)
_HR1_CACHE      = TTLCache(maxsize=256, ttl=1800)
_HR4_CACHE      = TTLCache(maxsize=256, ttl=3600)
_QUOTE_CACHE    = TTLCache(maxsize=256, ttl=5)
_LOCK           = threading.RLock()

_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="data-fetch")


def _fetch_bars(symbol, timeframe, limit):
    try:
        r = requests.get(
            DATA_URL.format(symbol),
            headers=HEADERS,
            params={"timeframe": timeframe, "limit": limit},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        return r.json().get("bars", [])
    except Exception as e:
        print("[data_fetcher] {} {} error: {}".format(symbol, timeframe, e))
        return None


def get_intraday(symbol):
    with _LOCK:
        cached = _INTRADAY_CACHE.get(symbol)
    if cached is not None:
        return cached
    bars = _fetch_bars(symbol, "5Min", 78)
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


def get_current_price(symbol):
    with _LOCK:
        cached = _QUOTE_CACHE.get(symbol)
    if cached is not None:
        return cached
    try:
        r = requests.get(QUOTE_URL.format(symbol), headers=HEADERS, timeout=5)
        if r.status_code != 200:
            return None
        q = r.json().get("quote", {})
        ap, bp = q.get("ap", 0), q.get("bp", 0)
        if ap and bp:
            price = round((ap + bp) / 2, 2)
            with _LOCK:
                _QUOTE_CACHE[symbol] = price
            return price
    except Exception:
        pass
    return None


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
