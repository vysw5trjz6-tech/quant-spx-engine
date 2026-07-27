"""market_calendar: session/early-close correctness.

The important test here is test_builtin_matches_exchange_calendars: it
sweeps the whole exchange_calendars range and asserts the no-network
fallback agrees with the package backend on every session and every close
time. That is what makes the fallback a real backend rather than a
degradation, so a rule drift shows up here instead of as a misfired
Databento sweep on a holiday.
"""
from datetime import date, datetime, timedelta

import pytest
import pytz

import market_calendar as mc


ET = pytz.timezone("America/New_York")


# ---------------------------------------------------------------------------
# Known-date spot checks (backend-independent)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("d,expected", [
    ("2026-07-27", True),    # ordinary Monday
    ("2026-07-25", False),   # Saturday
    ("2026-07-26", False),   # Sunday
    ("2026-07-03", False),   # Jul 4 falls Sat -> observed Fri Jul 3
    ("2026-01-01", False),   # New Year's Day
    ("2026-01-19", False),   # MLK Day
    ("2026-02-16", False),   # Washington's Birthday
    ("2026-04-03", False),   # Good Friday
    ("2026-05-25", False),   # Memorial Day
    ("2026-06-19", False),   # Juneteenth
    ("2026-09-07", False),   # Labor Day
    ("2026-11-26", False),   # Thanksgiving
    ("2026-12-25", False),   # Christmas
])
def test_known_sessions_and_holidays(d, expected):
    assert mc.is_session(d) is expected


@pytest.mark.parametrize("d", [
    "2026-11-27",   # day after Thanksgiving
    "2026-12-24",   # Christmas Eve
    "2024-07-03",   # July 3rd, a session that year
])
def test_known_early_closes(d):
    assert mc.is_session(d) is True
    assert mc.is_early_close(d) is True
    assert mc.session_close_hour(d) == 13.0
    assert mc.close_shift(d) == -3.0


@pytest.mark.parametrize("d", ["2026-07-27", "2026-12-23", "2026-11-25"])
def test_regular_sessions_close_at_four(d):
    assert mc.session_close_hour(d) == 16.0
    assert mc.is_early_close(d) is False
    assert mc.close_shift(d) == 0.0


def test_non_session_has_no_close_and_zero_shift():
    assert mc.session_close_hour("2026-12-25") is None
    assert mc.close_shift("2026-12-25") == 0.0
    assert mc.session_open_hour("2026-12-25") is None
    assert mc.session_close_dt("2026-12-25") is None


def test_saturday_new_year_does_not_close_prior_friday():
    # NYSE rule: a Saturday Jan 1 is the one fixed-date holiday that is
    # NOT pulled back to the preceding Friday.
    assert date(2022, 1, 1).weekday() == 5
    assert mc.is_session("2021-12-31") is True


# ---------------------------------------------------------------------------
# The cross-check that keeps the fallback honest
# ---------------------------------------------------------------------------

# One-off, non-recurring closures. exchange_calendars carries them; the
# rule-based fallback cannot derive them and is documented not to.
KNOWN_ONE_OFF_CLOSURES = {
    date(2007, 1, 2),    # Gerald Ford, national day of mourning
    date(2012, 10, 29),  # Hurricane Sandy
    date(2012, 10, 30),  # Hurricane Sandy
    date(2018, 12, 5),   # George H. W. Bush, national day of mourning
    date(2025, 1, 9),    # Jimmy Carter, national day of mourning
}


def test_builtin_matches_exchange_calendars():
    cal = mc._xnys()
    if cal is None:
        pytest.skip("exchange_calendars not installed")

    d, end = cal.first_session.date(), cal.last_session.date()
    session_mismatches, close_mismatches = [], []
    while d <= end:
        x = mc._xcals_lookup(d)
        b = mc._builtin_lookup(d)
        if x[0] != b[0]:
            session_mismatches.append(d)
        elif x[0] and x[1] != b[1]:
            close_mismatches.append((d, x[1], b[1]))
        d += timedelta(days=1)

    # Close times must agree on every single session -- there is no
    # excuse for an early-close divergence.
    assert close_mismatches == []
    # Session disagreements are allowed only for the one-off closures.
    assert set(session_mismatches) == KNOWN_ONE_OFF_CLOSURES


def test_builtin_covers_dates_outside_the_package_range():
    # exchange_calendars is built with a bounded range; far-future dates
    # must still resolve via the built-in rules rather than raising.
    far = date(2099, 12, 25)
    assert mc.is_session(far) is False
    far_session = date(2099, 12, 23)
    assert mc.is_session(far_session) is True
    assert mc.session_close_hour(far_session) == 16.0


# ---------------------------------------------------------------------------
# Clock + navigation
# ---------------------------------------------------------------------------

def test_is_open_now_respects_early_close():
    half_day = date(2026, 12, 24)
    at_1230 = ET.localize(datetime.combine(half_day, datetime.min.time()
                                           .replace(hour=12, minute=30)))
    at_1400 = ET.localize(datetime.combine(half_day, datetime.min.time()
                                           .replace(hour=14, minute=0)))
    assert mc.is_open_now(at_1230) is True
    assert mc.is_open_now(at_1400) is False   # closed at 13:00, not 16:00


def test_is_open_now_regular_session_bounds():
    d = date(2026, 7, 27)

    def at(h, m):
        return ET.localize(datetime.combine(
            d, datetime.min.time().replace(hour=h, minute=m)))

    assert mc.is_open_now(at(9, 29)) is False
    assert mc.is_open_now(at(9, 30)) is True
    assert mc.is_open_now(at(15, 59)) is True
    assert mc.is_open_now(at(16, 0)) is False


def test_is_open_now_false_on_holiday():
    xmas = ET.localize(datetime(2026, 12, 25, 11, 0))
    assert mc.is_open_now(xmas) is False


def test_session_close_dt_is_et_aware():
    dt = mc.session_close_dt("2026-12-24")
    assert dt.hour == 13 and dt.minute == 0
    assert dt.tzinfo is not None
    assert dt.utcoffset() is not None


def test_previous_and_next_session_skip_the_long_holiday_weekend():
    # Thu 2026-11-26 Thanksgiving, Fri 11-27 half day.
    assert mc.next_session("2026-11-25") == date(2026, 11, 27)
    assert mc.previous_session("2026-11-30") == date(2026, 11, 27)
    # Jul 4 2026 falls Sat: Fri Jul 3 is the observed holiday.
    assert mc.next_session("2026-07-02") == date(2026, 7, 6)
    assert mc.previous_session("2026-07-06") == date(2026, 7, 2)


def test_sessions_between_excludes_holidays_and_weekends():
    got = mc.sessions_between("2026-11-23", "2026-11-29")
    assert got == [date(2026, 11, 23), date(2026, 11, 24),
                   date(2026, 11, 25), date(2026, 11, 27)]


def test_accepts_str_date_and_datetime():
    d = date(2026, 7, 27)
    assert mc.is_session(d) is True
    assert mc.is_session("2026-07-27") is True
    assert mc.is_session(datetime(2026, 7, 27, 11, 30)) is True
