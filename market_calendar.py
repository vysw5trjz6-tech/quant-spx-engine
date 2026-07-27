# market_calendar.py
# Offline, holiday- AND early-close-aware US equity session calendar.
#
# Why this exists
# ---------------
# main.is_trading_day() asked Alpaca's calendar endpoint and fell back to
# `weekday() < 5` when the API was unreachable. That fallback fires the
# premarket brief, the EOD jobs and the *paid* Databento GEX/OI sweeps on
# market holidays.
#
# Worse, nothing anywhere handled EARLY CLOSES. On a 1:00 PM ET half-day
# (July 3, the Friday after Thanksgiving, Christmas Eve) the scheduler's
# post-close gates (16.05 paper replay, 16.25 IV/digest, 16.50 vol1d EOD)
# sit three hours past the actual close, and every "time to expiry to a
# 4:00 PM PM-settle" calculation in the 0DTE path is mis-scaled on exactly
# the sessions where 0DTE gamma is sharpest.
#
# Backends
# --------
#   1. exchange_calendars (Apache-2.0) XNYS calendar -- authoritative,
#      ships holidays + early closes as package data, no network call.
#   2. A built-in NYSE rule table -- used when exchange_calendars isn't
#      installed or the requested date is outside its bounded range.
#
# The built-in table is a real fallback, not a degradation: it encodes the
# NYSE holiday rules (including the Saturday/Sunday observance rules and
# Good Friday) and the three recurring half-days. tests/test_market_calendar
# cross-checks it against exchange_calendars across a multi-decade span, so
# a divergence is a test failure rather than a silent misfire in production.
#
# Everything returns plain Python types (bool / float / date). pandas never
# leaks out of this module.

import threading
from datetime import date, datetime, time as dtime, timedelta

import pytz

ET = pytz.timezone("America/New_York")

# Regular session bounds, ET hours as floats (9.5 == 9:30 AM).
REGULAR_OPEN_HOUR  = 9.5
REGULAR_CLOSE_HOUR = 16.0
EARLY_CLOSE_HOUR   = 13.0


# =============================================
# BACKEND 1 — exchange_calendars
# =============================================

_xcals_lock = threading.Lock()
_xcals_cal  = None
_xcals_tried = False
_xcals_error = None


def _xnys():
    """Lazily build the XNYS calendar. ~0.8s on first call, then cached.

    Returns None (once, sticky) if the package is missing or the build
    fails -- callers fall through to the built-in rules.
    """
    global _xcals_cal, _xcals_tried, _xcals_error
    if _xcals_tried:
        return _xcals_cal
    with _xcals_lock:
        if _xcals_tried:
            return _xcals_cal
        try:
            import exchange_calendars as xcals
            _xcals_cal = xcals.get_calendar("XNYS")
        except Exception as e:          # ImportError or a build failure
            _xcals_cal   = None
            _xcals_error = str(e)
        _xcals_tried = True
    return _xcals_cal


def _xcals_lookup(d):
    """(is_session, close_hour) from exchange_calendars, or None if the
    backend is unavailable or `d` is outside its bounded range."""
    cal = _xnys()
    if cal is None:
        return None
    try:
        # The calendar is built with a bounded range (~20y back, 1y
        # forward). Outside it, defer to the built-in rules.
        if not (cal.first_session.date() <= d <= cal.last_session.date()):
            return None
        stamp = d.isoformat()
        if not cal.is_session(stamp):
            return False, None
        close_et = cal.session_close(stamp).tz_convert(ET)
        return True, close_et.hour + close_et.minute / 60.0
    except Exception:
        return None


# =============================================
# BACKEND 2 — built-in NYSE rules
# =============================================

def _easter(year):
    """Gregorian Easter Sunday (Meeus/Jones/Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * l) // 433
    month = (h + l - 7 * m + 90) // 25
    day = (h + l - 7 * m + 33 * month + 19) % 32
    return date(year, month, day)


def _nth_weekday(year, month, weekday, n):
    """n-th `weekday` (Mon=0) of the month. n=-1 means the last one."""
    if n > 0:
        first = date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + timedelta(days=offset + 7 * (n - 1))
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d):
    """NYSE observance shift for a fixed-date holiday.

    Saturday -> the preceding Friday, Sunday -> the following Monday.
    """
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _builtin_holidays(year):
    """Set of full-closure NYSE dates for `year` (recurring rules only).

    Excludes one-off closures (presidential funerals, 9/11, Sandy). Those
    are in exchange_calendars, which is the primary backend; the built-in
    table is the no-network fallback and errs toward "open", never toward
    silently spending on a closed session -- Alpaca's live calendar is
    still consulted first for that.
    """
    days = set()

    # New Year's Day. A Saturday Jan 1 is NOT pulled back to Dec 31 --
    # the NYSE explicitly stays open that Friday (unlike every other
    # fixed-date holiday).
    ny = date(year, 1, 1)
    if ny.weekday() != 5:
        days.add(_observed(ny))

    days.add(_nth_weekday(year, 1, 0, 3))        # MLK Day        3rd Mon Jan
    days.add(_nth_weekday(year, 2, 0, 3))        # Washington's   3rd Mon Feb
    days.add(_easter(year) - timedelta(days=2))  # Good Friday
    days.add(_nth_weekday(year, 5, 0, -1))       # Memorial Day   last Mon May
    if year >= 2022:                             # Juneteenth (NYSE from 2022)
        days.add(_observed(date(year, 6, 19)))
    days.add(_observed(date(year, 7, 4)))        # Independence Day
    days.add(_nth_weekday(year, 9, 0, 1))        # Labor Day      1st Mon Sep
    days.add(_nth_weekday(year, 11, 3, 4))       # Thanksgiving   4th Thu Nov
    days.add(_observed(date(year, 12, 25)))      # Christmas
    return days


def _builtin_is_session(d):
    return d.weekday() < 5 and d not in _builtin_holidays(d.year)


def _builtin_early_close(d):
    """The three recurring 1:00 PM ET half-days.

    Each is expressed as "this specific date, when it is itself a
    session" -- which automatically handles the years where the date is
    the observed holiday instead (e.g. Fri 2026-07-03 is the observed
    Independence Day, so it is a full closure, not a half-day).
    """
    if not _builtin_is_session(d):
        return False
    if (d.month, d.day) == (7, 3):                       # July 3rd
        return True
    if (d.month, d.day) == (12, 24):                     # Christmas Eve
        return True
    thanksgiving = _nth_weekday(d.year, 11, 3, 4)        # day after Thanksgiving
    return d == thanksgiving + timedelta(days=1)


def _builtin_lookup(d):
    if not _builtin_is_session(d):
        return False, None
    return True, (EARLY_CLOSE_HOUR if _builtin_early_close(d)
                  else REGULAR_CLOSE_HOUR)


# =============================================
# PUBLIC API
# =============================================

_cache = {}
_cache_lock = threading.Lock()


def _coerce(d):
    if d is None:
        return datetime.now(ET).date()
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, str):
        return datetime.strptime(d[:10], "%Y-%m-%d").date()
    return d


def _lookup(d=None):
    """(is_session: bool, close_hour: float|None) for an ET calendar date."""
    d = _coerce(d)
    with _cache_lock:
        hit = _cache.get(d)
    if hit is not None:
        return hit

    res = _xcals_lookup(d)
    if res is None:
        res = _builtin_lookup(d)

    with _cache_lock:
        # Bounded: a long-lived process only ever touches a handful of
        # dates, but a caller sweeping history shouldn't grow this forever.
        if len(_cache) > 4096:
            _cache.clear()
        _cache[d] = res
    return res


def is_session(d=None):
    """True if `d` (default: today ET) is a US equity trading session."""
    return _lookup(d)[0]


def session_close_hour(d=None):
    """ET close as a float hour (16.0, or 13.0 on a half-day). None if
    `d` is not a session."""
    return _lookup(d)[1]


def session_open_hour(d=None):
    """ET open as a float hour. None if `d` is not a session. Early-close
    days open at the regular time -- only the close moves."""
    return REGULAR_OPEN_HOUR if is_session(d) else None


def is_early_close(d=None):
    """True if `d` is a session that closes before 4:00 PM ET."""
    is_sess, close_h = _lookup(d)
    return bool(is_sess and close_h is not None
                and close_h < REGULAR_CLOSE_HOUR)


def close_shift(d=None):
    """Hours to shift a 4:00-PM-relative schedule gate by, so that a gate
    written as `16.25` (4:15 PM) fires 15 minutes after the *actual*
    close.

    0.0 on a regular session or a non-session (nothing is scheduled then
    anyway); -3.0 on a 1:00 PM half-day.
    """
    close_h = session_close_hour(d)
    if close_h is None:
        return 0.0
    return close_h - REGULAR_CLOSE_HOUR


def session_close_dt(d=None):
    """Timezone-aware ET datetime of the close, or None if not a session."""
    d = _coerce(d)
    close_h = session_close_hour(d)
    if close_h is None:
        return None
    hour = int(close_h)
    minute = int(round((close_h - hour) * 60))
    return ET.localize(datetime.combine(d, dtime(hour, minute)))


def is_open_now(now_et=None):
    """Offline replacement for a market-clock API call. RTH only."""
    now_et = now_et or datetime.now(ET)
    is_sess, close_h = _lookup(now_et.date())
    if not is_sess:
        return False
    hour = now_et.hour + now_et.minute / 60.0 + now_et.second / 3600.0
    return REGULAR_OPEN_HOUR <= hour < close_h


def previous_session(d=None, n=1):
    """The n-th prior trading session strictly before `d`."""
    cur = _coerce(d)
    for _ in range(n):
        cur -= timedelta(days=1)
        guard = 0
        while not is_session(cur):
            cur -= timedelta(days=1)
            guard += 1
            if guard > 30:              # no 30-day US market closure exists
                return None
    return cur


def next_session(d=None, n=1):
    """The n-th trading session strictly after `d`."""
    cur = _coerce(d)
    for _ in range(n):
        cur += timedelta(days=1)
        guard = 0
        while not is_session(cur):
            cur += timedelta(days=1)
            guard += 1
            if guard > 30:
                return None
    return cur


def sessions_between(start, end):
    """Sorted list of session dates in the inclusive [start, end] range."""
    start, end = _coerce(start), _coerce(end)
    out, cur = [], start
    while cur <= end:
        if is_session(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


def backend():
    """'exchange_calendars' when the package backend is live, else
    'builtin'."""
    return "exchange_calendars" if _xnys() is not None else "builtin"


def status(d=None):
    """Diagnostic blob for /db-status and the boot log."""
    d = _coerce(d)
    is_sess, close_h = _lookup(d)
    return {
        "date":         d.isoformat(),
        "backend":      backend(),
        "backend_error": _xcals_error,
        "is_session":   is_sess,
        "close_hour":   close_h,
        "early_close":  bool(is_sess and close_h is not None
                             and close_h < REGULAR_CLOSE_HOUR),
        "close_shift":  close_shift(d),
    }
