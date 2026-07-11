# The business-time convention is THE VIX1D-specific correctness risk:
# calendar time silently mis-scales the whole level (a Friday 0DTE/Monday
# pair spans ~1 business day, not 3 calendar days). These tests pin the
# convention in vol1d/daycount.py — the single home for it.

from datetime import date, datetime

from vol1d import daycount


BD_YEAR_MIN = 252 * 1440.0


def test_zero_dte_morning_fraction():
    # Wednesday 2026-07-08 09:30 ET -> 4:00 PM settle = 390 minutes.
    now = datetime(2026, 7, 8, 9, 30)
    assert daycount.business_minutes_to_expiry(now, date(2026, 7, 8)) == 390.0
    t = daycount.business_time_to_expiry(now, date(2026, 7, 8))
    assert abs(t - 390.0 / BD_YEAR_MIN) < 1e-12


def test_one_full_business_day_is_one_252th():
    # Settle-to-settle across one business day = exactly 1/252 years.
    now = datetime(2026, 7, 8, 16, 0)   # Wed 4:00 PM
    t = daycount.business_time_to_expiry(now, date(2026, 7, 9))  # Thu
    assert abs(t - 1.0 / 252.0) < 1e-12


def test_friday_to_monday_skips_weekend():
    # Friday 2026-07-10 10:00 ET -> Monday 2026-07-13 settle.
    # Rest of Friday (840 min to midnight) + Monday midnight->16:00 (960).
    # Saturday/Sunday contribute NOTHING under business time.
    now = datetime(2026, 7, 10, 10, 0)
    minutes = daycount.business_minutes_to_expiry(now, date(2026, 7, 13))
    assert minutes == (1440 - 600) + 960
    # Calendar time would be ~3 days; business time must be ~1.25 days.
    assert minutes / 1440.0 < 1.5


def test_holiday_excluded_like_weekend():
    # Thu 2026-07-02 15:00 -> Mon 2026-07-06, with Fri 7/3 a market holiday:
    # rest of Thu (540) + no business days between + Monday 960.
    now = datetime(2026, 7, 2, 15, 0)
    holidays = {date(2026, 7, 3)}
    minutes = daycount.business_minutes_to_expiry(
        now, date(2026, 7, 6), holidays=holidays)
    assert minutes == 540 + 960
    # Without the holiday, Friday adds a full business day.
    assert daycount.business_minutes_to_expiry(
        now, date(2026, 7, 6)) == 540 + 1440 + 960


def test_past_settlement_clamps_to_zero():
    now = datetime(2026, 7, 8, 16, 30)
    assert daycount.business_minutes_to_expiry(now, date(2026, 7, 8)) <= 0
    assert daycount.business_time_to_expiry(now, date(2026, 7, 8)) == 0.0


def test_weekend_observation_contributes_no_current_day_time():
    # A Saturday timestamp (replay edge case): only Monday's 960 minutes
    # remain -- the weekend itself must not count.
    now = datetime(2026, 7, 11, 12, 0)   # Saturday
    minutes = daycount.business_minutes_to_expiry(now, date(2026, 7, 13))
    assert minutes == 960


def test_business_days_between_exclusive():
    assert daycount.business_days_between(date(2026, 7, 6), date(2026, 7, 10)) == 3
    assert daycount.business_days_between(date(2026, 7, 10), date(2026, 7, 13)) == 0
    assert daycount.business_days_between(date(2026, 7, 10), date(2026, 7, 10)) == 0


def test_holiday_accepts_iso_strings():
    assert not daycount.is_business_day(date(2026, 7, 3), {"2026-07-03"})
    assert daycount.is_business_day(date(2026, 7, 2), {"2026-07-03"})
