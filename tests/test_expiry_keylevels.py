"""Tests for the weekly expiry policy and the key-levels memoization."""
import datetime as dt

import pytz

import main
import key_levels


ET = pytz.timezone("America/New_York")


def _expiry(y, m, d, hh=10, mm=0):
    now = ET.localize(dt.datetime(y, m, d, hh, mm))
    return main.current_week_expiry(now_et=now, zero_dte_cutoff=14.5)


def test_weekday_returns_this_week_friday():
    # 2026-06-01 is a Monday; this week's Friday is 2026-06-05.
    expiry, dte = _expiry(2026, 6, 1)
    assert expiry == "2026-06-05"
    assert dte == 4
    # Thursday -> still this Friday, 1 DTE.
    expiry, dte = _expiry(2026, 6, 4)
    assert expiry == "2026-06-05"
    assert dte == 1


def test_friday_before_cutoff_is_zero_dte():
    expiry, dte = _expiry(2026, 6, 5, hh=10)
    assert expiry == "2026-06-05"
    assert dte == 0


def test_friday_after_cutoff_rolls_to_next_friday():
    expiry, dte = _expiry(2026, 6, 5, hh=15)
    assert expiry == "2026-06-12"
    assert dte == 7


def test_weekend_rolls_to_next_friday():
    # Saturday 2026-06-06 -> next Friday 2026-06-12.
    expiry, dte = _expiry(2026, 6, 6)
    assert expiry == "2026-06-12"
    assert dte == 6


def _bars():
    base = [{"t": "2026-05-{:02d}".format(d), "o": 100 + d, "h": 102 + d,
             "l": 98 + d, "c": 100 + d, "v": 1_000_000} for d in range(1, 21)]
    return base


def test_key_levels_memoized(monkeypatch):
    key_levels.clear_cache()
    calls = {"n": 0}

    def _fake_iv(symbol, price):
        calls["n"] += 1
        return 0.25

    monkeypatch.setattr(key_levels.iv_rank, "fetch_atm_iv", _fake_iv)

    bars = _bars()
    kl1 = key_levels.get_key_levels("XYZ", daily_bars=bars, spot=110.0,
                                    week_expiry="2026-06-05", dte=4)
    kl2 = key_levels.get_key_levels("XYZ", daily_bars=bars, spot=110.0,
                                    week_expiry="2026-06-05", dte=4)
    # Second call within the cycle must be a pure cache hit -- no extra IV fetch.
    assert calls["n"] == 1
    assert kl1 is kl2
    assert kl1.atm_iv == 0.25
    assert kl1.expected_move_1sd is not None
    assert kl1.swing_high >= kl1.swing_low
