"""Tests for the one-per-tier active-position alert gate + live monitor."""
import os
import tempfile

import main


def _fresh_db():
    fd, path = tempfile.mkstemp(prefix="trades-", suffix=".db")
    os.close(fd)
    main.DB_FILE = path
    main.init_db()
    return path


def test_one_position_per_tier_lock():
    _fresh_db()
    assert not main.tier_has_open_position("INTRADAY")
    assert not main.tier_has_open_position("WEEKLY")

    main.open_auto_position({
        "symbol": "SPY", "direction": "CALL", "price": 500.0,
        "premium": 1.2, "stop": 0.66, "target": 1.68, "horizon": "INTRADAY",
        "und_call_t1": 505.0, "und_call_t2": 510.0, "und_call_stop": 497.0,
    })
    # Intraday tier now locked; weekly tier independent (still open).
    assert main.tier_has_open_position("INTRADAY")
    assert not main.tier_has_open_position("WEEKLY")

    main.open_auto_position({
        "symbol": "NVDA", "direction": "CALL", "price": 100.0,
        "premium": 2.0, "stop": 1.1, "target": 2.8, "horizon": "WEEKLY",
        "und_call_t1": 110.0, "und_call_stop": 96.0,
    })
    assert main.tier_has_open_position("WEEKLY")


def test_monitor_closes_win_on_target_touch(monkeypatch):
    _fresh_db()
    main.open_auto_position({
        "symbol": "SPY", "direction": "CALL", "price": 500.0,
        "premium": 1.2, "stop": 0.66, "target": 1.68, "horizon": "INTRADAY",
        "und_call_t1": 505.0, "und_call_stop": 497.0,
    })

    # Underlying prints a high above T1 -> WIN, tier frees up.
    bars = [{"t": "2026-06-01T14:00:00Z", "o": 500, "h": 506, "l": 499, "c": 505, "v": 1}]
    monkeypatch.setattr(main, "get_intraday", lambda s: bars)
    monkeypatch.setattr(main, "get_current_price", lambda s: 505.0)

    main.monitor_active_positions()
    assert not main.tier_has_open_position("INTRADAY")
    closed = [t for t in main.db_get_today_trades() if t["symbol"] == "SPY"]
    assert closed and closed[0]["outcome"] == "WIN"


def test_monitor_closes_loss_on_stop_touch(monkeypatch):
    _fresh_db()
    main.open_auto_position({
        "symbol": "QQQ", "direction": "CALL", "price": 400.0,
        "premium": 1.0, "stop": 0.55, "target": 1.4, "horizon": "INTRADAY",
        "und_call_t1": 406.0, "und_call_stop": 397.0,
    })
    bars = [{"t": "2026-06-01T14:00:00Z", "o": 400, "h": 401, "l": 396, "c": 397, "v": 1}]
    monkeypatch.setattr(main, "get_intraday", lambda s: bars)
    monkeypatch.setattr(main, "get_current_price", lambda s: 397.0)

    main.monitor_active_positions()
    assert not main.tier_has_open_position("INTRADAY")
    closed = [t for t in main.db_get_today_trades() if t["symbol"] == "QQQ"]
    assert closed and closed[0]["outcome"] == "LOSS"


def test_monitor_leaves_manual_trades_alone(monkeypatch):
    _fresh_db()
    # Manual trade occupies the tier but must NOT be auto-resolved.
    main.db_log_trade("SPY", "CALL", 1.2, stop=0.66, target=1.68,
                      entry_under=500.0, und_stop=497.0, und_target_t1=505.0,
                      horizon="INTRADAY", mode="manual")
    bars = [{"t": "2026-06-01T14:00:00Z", "o": 500, "h": 510, "l": 499, "c": 508, "v": 1}]
    monkeypatch.setattr(main, "get_intraday", lambda s: bars)
    monkeypatch.setattr(main, "get_current_price", lambda s: 508.0)

    main.monitor_active_positions()
    # Still open (manual trades are closed only by the user) -> tier still locked.
    assert main.tier_has_open_position("INTRADAY")
