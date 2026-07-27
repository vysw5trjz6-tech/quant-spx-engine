"""main.py's session helpers and the early-close shift applied to the
scheduler's post-close gates.

The scheduler itself is an infinite loop, so what's pinned here is the
arithmetic it runs on: that `close_shift()` slides a 4:00-PM-relative gate
onto the actual close, and that the session helpers degrade sanely when
the calendar module is missing.
"""
from datetime import date, datetime

import pytest
import pytz

import main
import market_calendar


ET = pytz.timezone("America/New_York")

REGULAR   = date(2026, 7, 27)    # ordinary Monday
HALF_DAY  = date(2026, 12, 24)   # Christmas Eve, 1:00 PM close
HOLIDAY   = date(2026, 12, 25)   # Christmas


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_session_close_hour_tracks_the_real_close():
    assert main.session_close_hour(REGULAR) == 16.0
    assert main.session_close_hour(HALF_DAY) == 13.0


def test_close_shift_zero_on_a_regular_session():
    assert main.close_shift(REGULAR) == 0.0


def test_close_shift_pulls_gates_back_three_hours_on_a_half_day():
    assert main.close_shift(HALF_DAY) == -3.0


def test_close_shift_zero_on_a_non_session():
    # Nothing is scheduled on a holiday (every gate is _session-guarded),
    # so a neutral shift is the right answer rather than None.
    assert main.close_shift(HOLIDAY) == 0.0


def test_is_early_close():
    assert main.is_early_close(HALF_DAY) is True
    assert main.is_early_close(REGULAR) is False
    assert main.is_early_close(HOLIDAY) is False


def test_helpers_degrade_to_regular_hours_without_the_calendar(monkeypatch):
    monkeypatch.setattr(main, "HAS_MARKET_CALENDAR", False)
    assert main.session_close_hour(HALF_DAY) == 16.0
    assert main.close_shift(HALF_DAY) == 0.0
    assert main.is_early_close(HALF_DAY) is False


# ---------------------------------------------------------------------------
# Gate arithmetic — the reason close_shift exists
# ---------------------------------------------------------------------------

# (gate label, the 4:00-PM-relative hour written in background_scheduler)
POST_CLOSE_GATES = [
    ("paper replay",  16.05),
    ("friday AI",     16.08),
    ("IV snapshot",   16.25),
    ("friday digest", 16.25),
    ("vol1d EOD",     16.50),
]


@pytest.mark.parametrize("label,gate", POST_CLOSE_GATES)
def test_post_close_gates_fire_after_the_actual_close(label, gate):
    shift = main.close_shift(HALF_DAY)
    fires_at = gate + shift
    close_at = main.session_close_hour(HALF_DAY)

    assert fires_at > close_at, "{} would fire before the close".format(label)
    # Same offset past the close as on a regular day -- the gate keeps its
    # intended meaning ("N minutes after the bell"), it just moves.
    assert fires_at - close_at == pytest.approx(gate - 16.0, abs=1e-9)


@pytest.mark.parametrize("label,gate", POST_CLOSE_GATES)
def test_unshifted_gates_would_have_missed_the_half_day_close(label, gate):
    # Documents the bug: without the shift every post-close job sat ~3h
    # past a 1:00 PM close.
    close_at = main.session_close_hour(HALF_DAY)
    assert gate - close_at > 3.0


@pytest.mark.parametrize("label,gate", POST_CLOSE_GATES)
def test_gates_are_unchanged_on_a_regular_session(label, gate):
    assert gate + main.close_shift(REGULAR) == gate


def test_pre_close_gates_also_shift():
    # The overnight gamma-reversal window (15.75-15.93) is "the last ~15
    # minutes of the session"; on a half-day it must land before 13:00.
    shift = main.close_shift(HALF_DAY)
    start, end = 15.75 + shift, 15.93 + shift
    close_at = main.session_close_hour(HALF_DAY)
    assert start < end <= close_at
    assert close_at - end == pytest.approx(16.0 - 15.93, abs=1e-9)


# ---------------------------------------------------------------------------
# is_trading_day fallback path
# ---------------------------------------------------------------------------

def test_is_trading_day_falls_back_to_the_offline_calendar(monkeypatch):
    """With Alpaca unreachable, a holiday must read as a non-session.

    The old fallback was `weekday() < 5`, which said True on Christmas and
    let the paid Databento sweeps fire against a closed market.
    """
    def _boom(*a, **kw):
        raise OSError("network down")

    monkeypatch.setattr(main.requests, "get", _boom)
    monkeypatch.setattr(main, "_trading_day_cache", {"date": None, "val": None})

    # Christmas 2026 (a Friday -- the weekday heuristic would say True).
    assert HOLIDAY.weekday() < 5
    monkeypatch.setattr(market_calendar, "is_session", lambda d=None: False)
    assert main.is_trading_day() is False


def test_market_open_uses_the_offline_clock_when_the_api_is_down(monkeypatch):
    def _boom(*a, **kw):
        raise OSError("network down")

    monkeypatch.setattr(main.requests, "get", _boom)

    # 2:00 PM on a half-day: closed at 13:00, but the old fixed
    # 9:30-16:00 fallback held it open.
    at_1400 = ET.localize(datetime(2026, 12, 24, 14, 0))
    assert market_calendar.is_open_now(at_1400) is False

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return at_1400

    monkeypatch.setattr(main, "datetime", _FrozenDatetime)
    assert main.market_open() is False
