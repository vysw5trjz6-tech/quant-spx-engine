"""Tests for the Monday-open logic hardening:
  * AI training floor (ignore pre-cutoff trades regardless of learning_epoch)
  * broad-tape trend helper for the weekly directional gate
  * the tightened policy defaults (trend-aligned, A/B-only alerts)
"""
import os
import tempfile

import db_utils
import main


def _fresh_db():
    fd, path = tempfile.mkstemp(prefix="trades-", suffix=".db")
    os.close(fd)
    main.DB_FILE = path
    main.init_db()
    return path


def _insert_closed(ts, outcome="WIN"):
    conn = db_utils.connect(main.DB_FILE)
    conn.execute(
        "INSERT INTO trades (ts,symbol,direction,premium,outcome,r_mult,grade) "
        "VALUES (?,?,?,?,?,?,?)",
        (ts, "SPY", "CALL", 1.0, outcome, 1.0, "A"))
    conn.commit()
    conn.close()


def test_ai_training_floor_excludes_pre_cutoff(monkeypatch):
    _fresh_db()
    # Empty learning_epoch -> the hard floor must still apply.
    monkeypatch.setitem(main._scanner_config, "learning_epoch", "")
    _insert_closed("2026-05-01T15:00:00+00:00")   # before floor -> excluded
    _insert_closed("2026-05-29T15:00:00+00:00")   # on/after floor -> kept
    _insert_closed("2026-06-02T15:00:00+00:00")   # after floor -> kept
    rows = main.db_get_all_closed_trades()
    assert len(rows) == 2
    assert all(r["ts"] >= main.AI_TRAINING_FLOOR for r in rows)


def test_learning_epoch_later_than_floor_wins(monkeypatch):
    _fresh_db()
    # A learning_epoch AFTER the floor should further restrict, not loosen.
    monkeypatch.setitem(main._scanner_config, "learning_epoch",
                        "2026-06-01T00:00:00+00:00")
    _insert_closed("2026-05-29T15:00:00+00:00")   # after floor, before epoch -> excluded
    _insert_closed("2026-06-02T15:00:00+00:00")   # after epoch -> kept
    rows = main.db_get_all_closed_trades()
    assert len(rows) == 1
    assert rows[0]["ts"] == "2026-06-02T15:00:00+00:00"


def test_market_trend_up_down_flat(monkeypatch):
    def _bars(seq):
        return [{"c": c} for c in seq]

    # Rising series, price above a rising 50DMA -> UP
    main._market_trend_cache.update({"ts": 0, "trend": None})
    monkeypatch.setattr(main, "get_daily_extended",
                        lambda s, limit=80: _bars([100 + i for i in range(80)]))
    assert main.market_trend() == "UP"

    # Falling series -> DOWN
    main._market_trend_cache.update({"ts": 0, "trend": None})
    monkeypatch.setattr(main, "get_daily_extended",
                        lambda s, limit=80: _bars([200 - i for i in range(80)]))
    assert main.market_trend() == "DOWN"

    # Too little data -> FLAT (safe default)
    main._market_trend_cache.update({"ts": 0, "trend": None})
    monkeypatch.setattr(main, "get_daily_extended",
                        lambda s, limit=80: _bars([100, 101, 102]))
    assert main.market_trend() == "FLAT"


def test_policy_defaults():
    cfg = main.DEFAULT_CONFIG
    # Trade with the tape, alert only A/B, weekly needs the broad tape onside.
    assert cfg["counter_trend_allowed"] is False
    assert cfg["alert_min_grade"] == "B"
    assert cfg["weekly_require_uptrend"] is True
    assert 0 < cfg["max_breakout_extension"] <= 1.0
