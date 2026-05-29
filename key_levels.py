"""Per-cycle shared key-levels cache.

Fib levels, swing highs/lows and ATR were previously recomputed on every call,
and ATM IV hit the options feed per detector. This module computes them ONCE
per symbol per scan cycle and memoizes the result, so detectors, the
expected-move target framework, the rationale builder, the index-context block
and the dashboard all read the same numbers instead of refetching.

Caching is keyed on (symbol, latest_daily_bar_timestamp, dte): a new daily bar
or a roll to a new weekly expiry invalidates the entry; otherwise repeated
calls within a cycle are free. IV is the one network-backed field
(iv_rank.fetch_atm_iv); if it fails the level object still returns with iv=None
so callers degrade to fib-only targets.

Kept import-light (no dependency on main) to avoid a circular import.
"""
import math
import statistics
import threading
import time
from dataclasses import dataclass, field

try:
    import iv_rank
    _HAS_IV = True
except Exception:  # pragma: no cover
    _HAS_IV = False


# Mirror main.py's fib ratios so cached levels match the inline ones.
_FIBO_RETRACE = [0.236, 0.382, 0.500, 0.618, 0.786]
_FIBO_EXTEND  = [1.000, 1.272, 1.618, 2.000, 2.618]

_CACHE      = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL  = 30 * 60          # safety bound; primary key is the bar timestamp


@dataclass
class KeyLevels:
    symbol: str
    spot: float = None
    atr: float = None
    swing_high: float = None
    swing_low: float = None
    fib_retrace: dict = field(default_factory=dict)
    fib_extend: dict = field(default_factory=dict)
    atm_iv: float = None
    expected_move_1sd: float = None
    week_expiry: str = None
    dte: int = None
    computed_at: float = 0.0


def _atr(bars, n=14):
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return None
    return round(statistics.mean(trs[-n:]) if len(trs) >= n else statistics.mean(trs), 2)


def _fib(swing_low, swing_high, direction="CALL"):
    rng = swing_high - swing_low
    if rng <= 0:
        return {}, {}
    retrace, extend = {}, {}
    if direction == "CALL":
        for r in _FIBO_RETRACE:
            retrace[r] = round(swing_high - rng * r, 2)
        for e in _FIBO_EXTEND:
            extend[e] = round(swing_low + rng * e, 2)
    else:
        for r in _FIBO_RETRACE:
            retrace[r] = round(swing_low + rng * r, 2)
        for e in _FIBO_EXTEND:
            extend[e] = round(swing_high - rng * e, 2)
    return retrace, extend


def _bar_stamp(daily_bars):
    if not daily_bars:
        return None
    return daily_bars[-1].get("t")


def get_key_levels(symbol, daily_bars=None, spot=None, direction="CALL",
                   week_expiry=None, dte=None):
    """Return a cached KeyLevels for `symbol`.

    daily_bars: list of {t,o,h,l,c,v} (already cached upstream by data_fetcher).
    spot: current underlying price (defaults to last close).
    week_expiry/dte: from main.current_week_expiry; used for the expected move.

    Memoized per (symbol, last_bar_t, dte, direction). Never raises -- on any
    failure it returns a best-effort object (possibly with None fields).
    """
    stamp = _bar_stamp(daily_bars)
    cache_key = (symbol, stamp, dte, direction)
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(cache_key)
        if hit and (now - hit.computed_at) <= _CACHE_TTL:
            return hit

    kl = KeyLevels(symbol=symbol, week_expiry=week_expiry, dte=dte,
                   computed_at=now)
    try:
        if daily_bars and len(daily_bars) >= 10:
            recent = daily_bars[-20:]
            kl.swing_high = max(b["h"] for b in recent)
            kl.swing_low  = min(b["l"] for b in recent)
            kl.atr = _atr(daily_bars)
            kl.fib_retrace, kl.fib_extend = _fib(
                kl.swing_low, kl.swing_high, direction)
            if spot is None:
                spot = daily_bars[-1].get("c")
        kl.spot = spot

        if _HAS_IV and spot:
            try:
                kl.atm_iv = iv_rank.fetch_atm_iv(symbol, spot)
            except Exception:
                kl.atm_iv = None

        if kl.atm_iv and spot and dte and dte > 0:
            kl.expected_move_1sd = round(
                spot * kl.atm_iv * math.sqrt(dte / 252.0), 2)
    except Exception:
        pass

    with _CACHE_LOCK:
        _CACHE[cache_key] = kl
    return kl


def clear_cache():
    """Drop all cached levels (used by tests)."""
    with _CACHE_LOCK:
        _CACHE.clear()
