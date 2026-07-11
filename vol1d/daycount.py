# vol1d/daycount.py
# THE day-count convention for the VIX1D proxy, in one place.
#
# VIX1D measures time to expiration in BUSINESS days / a 252-business-day
# year — not calendar time. This is the key deviation from standard VIX
# math and the classic silent mis-scaling bug: a Friday-afternoon 0DTE/1DTE
# pair spans ~1 business day, not 3 calendar days. Every T fed into the
# variance formula must come from business_time_to_expiry() here.
#
# Convention:
#   * A business day is a weekday not in the supplied holiday set.
#   * Time is counted in minutes. Each intervening business day contributes
#     a full MINUTES_PER_DAY; weekend/holiday days contribute NOTHING.
#   * On the expiry day, time runs midnight -> settlement (SPXW settles
#     4:00 PM ET). On the observation day, time runs now -> midnight
#     (or now -> settlement when expiry is today).
#   * T (years) = business minutes / (business_day_year * MINUTES_PER_DAY).
#
# So one full business day from settle to settle is exactly 1/252 years,
# and a 0DTE observed at 9:30 AM ET has T = 390 / (252*1440).

from datetime import date, datetime, timedelta

import pytz

ET = pytz.timezone("America/New_York")

MINUTES_PER_DAY = 1440.0


def is_business_day(d, holidays=frozenset()):
    """Weekday and not a listed holiday. `holidays` is a set of date objects
    (or ISO 'YYYY-MM-DD' strings)."""
    if d.weekday() >= 5:
        return False
    return d not in holidays and d.isoformat() not in holidays


def business_days_between(d1, d2, holidays=frozenset()):
    """Business days STRICTLY between d1 and d2 (both exclusive)."""
    if d2 <= d1:
        return 0
    n = 0
    d = d1 + timedelta(days=1)
    while d < d2:
        if is_business_day(d, holidays):
            n += 1
        d += timedelta(days=1)
    return n


def business_minutes_to_expiry(now_et, expiry_date, settle_hour_et=16.0,
                                holidays=frozenset()):
    """Business minutes from `now_et` (aware or naive ET datetime) to
    settlement on `expiry_date`. Returns <= 0 when settlement has passed."""
    if now_et.tzinfo is not None:
        now_et = now_et.astimezone(ET).replace(tzinfo=None)
    if isinstance(expiry_date, datetime):
        expiry_date = expiry_date.date()

    settle_min = settle_hour_et * 60.0
    now_min    = now_et.hour * 60.0 + now_et.minute + now_et.second / 60.0

    if now_et.date() == expiry_date:
        return settle_min - now_min
    if now_et.date() > expiry_date:
        return 0.0

    # Rest of today (only if today is a business day — an off-day
    # observation, e.g. a weekend replay timestamp, contributes nothing).
    minutes = (MINUTES_PER_DAY - now_min) if is_business_day(now_et.date(), holidays) else 0.0
    # Full intervening business days.
    minutes += MINUTES_PER_DAY * business_days_between(now_et.date(), expiry_date, holidays)
    # Midnight -> settlement on expiry day.
    minutes += settle_min
    return minutes


def business_time_to_expiry(now_et, expiry_date, business_day_year=252,
                             settle_hour_et=16.0, holidays=frozenset()):
    """T in years under the business-day convention. The single entry point
    the proxy uses."""
    minutes = business_minutes_to_expiry(now_et, expiry_date,
                                         settle_hour_et, holidays)
    return max(minutes, 0.0) / (business_day_year * MINUTES_PER_DAY)
