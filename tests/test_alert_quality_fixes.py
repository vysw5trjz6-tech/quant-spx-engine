"""Regression tests for the alert-quality fixes.

Covers:
1. data_fetcher._fetch_bars always sends an explicit `start` (Alpaca defaults
   it to the beginning of the current day, which starved every history
   consumer down to a single session of bars).
2. _underlying_extent never widens to pre-entry session bars (the old
   `or intra` fallback read an ORB breakout's stop as already touched by the
   morning's range and force-closed every auto position one scan after entry).
3. get_premarket_gap takes "yesterday's close" from a prior-day bar even when
   the daily series ends with today's still-forming bar.
4. check_clear_air flags an empty level list as no_data, and the rank score
   treats it as neutral instead of granting the clear-to-T2 bonus.
5. The alert_min_t1_prob expectancy floor exists in the baseline config.
"""
import os
import tempfile
from datetime import datetime, timedelta

import data_fetcher
import main


def _fresh_db():
    fd, path = tempfile.mkstemp(prefix="trades-", suffix=".db")
    os.close(fd)
    main.DB_FILE = path
    main.init_db()
    return path


# ---------------------------------------------------------------------------
# 1. Explicit start window on every bars fetch
# ---------------------------------------------------------------------------

class _Resp:
    status_code = 200

    def json(self):
        return {"bars": []}


def _capture_params(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured.update(params or {})
        return _Resp()

    monkeypatch.setattr(data_fetcher.requests, "get", fake_get)
    return captured


def test_fetch_bars_daily_sends_wide_start(monkeypatch):
    captured = _capture_params(monkeypatch)
    data_fetcher._fetch_bars("FAKE", "1Day", 20)
    assert "start" in captured
    start_dt = datetime.fromisoformat(captured["start"])
    age = datetime.now(start_dt.tzinfo) - start_dt
    # 20 daily bars need ~28 calendar days minimum; window must cover them.
    assert age > timedelta(days=28)


def test_fetch_bars_hourly_sends_wide_start(monkeypatch):
    captured = _capture_params(monkeypatch)
    data_fetcher._fetch_bars("FAKE", "1Hour", 60)
    assert "start" in captured
    start_dt = datetime.fromisoformat(captured["start"])
    age = datetime.now(start_dt.tzinfo) - start_dt
    # 60 hourly bars ~ 9+ RTH sessions ~ 13+ calendar days.
    assert age > timedelta(days=13)


def test_fetch_bars_caller_start_is_respected(monkeypatch):
    captured = _capture_params(monkeypatch)
    data_fetcher._fetch_bars("FAKE", "5Min", 78, start="2026-06-11T09:30:00-04:00")
    assert captured["start"] == "2026-06-11T09:30:00-04:00"


# ---------------------------------------------------------------------------
# 2. Position monitor must ignore pre-entry bars
# ---------------------------------------------------------------------------

def test_monitor_ignores_pre_entry_session_range(monkeypatch):
    """Morning range above a PUT's stop must NOT stop the trade out when the
    only available bars predate the entry."""
    _fresh_db()
    main.open_auto_position({
        "symbol": "META", "direction": "PUT", "price": 561.0,
        "premium": 4.7, "stop": 2.59, "target": 6.58, "horizon": "INTRADAY",
        "und_put_t1": 554.0, "und_put_t2": 547.0, "und_put_stop": 568.0,
    })

    # Only bar predates entry (entry ts is "now"): the morning ORB high sits
    # above the stop. The old full-session fallback closed this as LOSS.
    pre_entry = [{"t": "2020-01-02T15:00:00Z",
                  "o": 565.0, "h": 570.0, "l": 561.0, "c": 562.0, "v": 1}]
    monkeypatch.setattr(main, "get_intraday", lambda s: pre_entry)
    # Live price between T1 and stop: nothing touched yet.
    monkeypatch.setattr(main, "get_current_price", lambda s: 560.0)

    main.monitor_active_positions()
    assert main.tier_has_open_position("INTRADAY")  # still OPEN


def test_monitor_still_closes_on_post_entry_touch(monkeypatch):
    _fresh_db()
    main.open_auto_position({
        "symbol": "META", "direction": "PUT", "price": 561.0,
        "premium": 4.7, "stop": 2.59, "target": 6.58, "horizon": "INTRADAY",
        "und_put_t1": 554.0, "und_put_t2": 547.0, "und_put_stop": 568.0,
    })

    # Post-entry bar (far-future ts sorts after the entry timestamp) tags T1.
    post_entry = [{"t": "2099-01-02T15:00:00Z",
                   "o": 560.0, "h": 561.0, "l": 553.0, "c": 554.5, "v": 1}]
    monkeypatch.setattr(main, "get_intraday", lambda s: post_entry)
    monkeypatch.setattr(main, "get_current_price", lambda s: 554.5)

    main.monitor_active_positions()
    assert not main.tier_has_open_position("INTRADAY")
    closed = [t for t in main.db_get_today_trades() if t["symbol"] == "META"]
    assert closed and closed[0]["outcome"] == "WIN"


def test_monitor_falls_back_to_current_price(monkeypatch):
    """No post-entry bars at all -> resolve against the live quote only."""
    _fresh_db()
    main.open_auto_position({
        "symbol": "QQQ", "direction": "CALL", "price": 700.0,
        "premium": 1.0, "stop": 0.55, "target": 1.4, "horizon": "INTRADAY",
        "und_call_t1": 705.0, "und_call_t2": 710.0, "und_call_stop": 695.0,
    })
    monkeypatch.setattr(main, "get_intraday", lambda s: [])
    monkeypatch.setattr(main, "get_current_price", lambda s: 706.0)

    main.monitor_active_positions()
    closed = [t for t in main.db_get_today_trades() if t["symbol"] == "QQQ"]
    assert closed and closed[0]["outcome"] == "WIN"


# ---------------------------------------------------------------------------
# 3. Premarket gap vs today's still-forming daily bar
# ---------------------------------------------------------------------------

def test_premarket_gap_skips_todays_forming_bar():
    daily = [
        {"t": "2026-06-10T04:00:00Z", "o": 99.0, "h": 101.0, "l": 98.0,
         "c": 100.0, "v": 1},
        # Today's forming bar: close == latest price, NOT yesterday's close.
        {"t": "2026-06-11T04:00:00Z", "o": 102.0, "h": 102.5, "l": 95.0,
         "c": 95.5, "v": 1},
    ]
    intraday = [{"t": "2026-06-11T13:30:00Z", "o": 102.0, "h": 102.5,
                 "l": 101.5, "c": 102.2, "v": 1}]
    gap, direction = main.get_premarket_gap(daily, intraday)
    # vs prior-day close 100.0 -> +2.0% gap UP. The old code compared against
    # today's forming close (95.5) and reported a phantom +6.8% "gap".
    assert gap == 2.0
    assert direction == "UP"


def test_premarket_gap_no_prior_day_is_flat():
    daily = [{"t": "2026-06-11T04:00:00Z", "o": 102.0, "h": 102.5, "l": 95.0,
              "c": 95.5, "v": 1}]
    intraday = [{"t": "2026-06-11T13:30:00Z", "o": 102.0, "h": 102.5,
                 "l": 101.5, "c": 102.2, "v": 1}]
    gap, direction = main.get_premarket_gap(daily, intraday)
    assert gap == 0.0
    assert direction == "FLAT"


# ---------------------------------------------------------------------------
# 4. Empty key-level list is neutral, not "clear to T2"
# ---------------------------------------------------------------------------

def test_clear_air_empty_levels_flags_no_data():
    ca = main.check_clear_air(100.0, "CALL", 102.0, 104.0, [])
    assert ca.get("no_data") is True
    assert ca["clear_to_t1"] and ca["clear_to_t2"]  # not demoted, just neutral


def test_rank_score_skips_clear_air_bonus_on_no_data():
    cfg = main.get_config()
    base = {"grade_pts": 50, "aligned": True, "direction": "CALL",
            "trend_1hr": "MIXED", "trend_score": 0.0, "time_vol_ratio": 1.0,
            "late_entry": False}

    no_data = dict(base, clear_air=main.check_clear_air(
        100.0, "CALL", 102.0, 104.0, []))
    truly_clear = dict(base, clear_air=main.check_clear_air(
        100.0, "CALL", 102.0, 104.0,
        [{"price": 110.0, "label": "PDH", "tf": "Daily", "strength": 3}]))

    diff = main.compute_rank_score(truly_clear) - main.compute_rank_score(no_data)
    assert diff == cfg["rank_clear_t2"]


# ---------------------------------------------------------------------------
# 5. Expectancy floor present in baseline config
# ---------------------------------------------------------------------------

def test_alert_min_t1_prob_in_baseline_config():
    assert main.get_config().get("alert_min_t1_prob") == 40
