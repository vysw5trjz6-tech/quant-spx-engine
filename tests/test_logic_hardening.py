"""Tests for the Monday-open logic hardening:
  * AI training floor (ignore pre-cutoff trades regardless of learning_epoch)
  * broad-tape trend helper for the weekly directional gate
  * the tightened policy defaults (trend-aligned, A/B-only alerts)
"""
import os
import tempfile
from datetime import datetime, timedelta

import db_utils
import main


def _fresh_db():
    fd, path = tempfile.mkstemp(prefix="trades-", suffix=".db")
    os.close(fd)
    main.DB_FILE = path
    main.init_db()
    return path


def _floor_offset(days):
    """ISO timestamp `days` relative to the hard AI training floor, so these
    tests keep working whenever the floor is bumped."""
    floor = datetime.fromisoformat(main.AI_TRAINING_FLOOR)
    return (floor + timedelta(days=days)).isoformat()


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
    _insert_closed(_floor_offset(-28))   # before floor -> excluded
    _insert_closed(_floor_offset(0))     # on/after floor -> kept
    _insert_closed(_floor_offset(4))     # after floor -> kept
    rows = main.db_get_all_closed_trades()
    assert len(rows) == 2
    assert all(r["ts"] >= main.AI_TRAINING_FLOOR for r in rows)


def test_learning_epoch_later_than_floor_wins(monkeypatch):
    _fresh_db()
    # A learning_epoch AFTER the floor should further restrict, not loosen.
    epoch = _floor_offset(3)
    kept  = _floor_offset(5)
    monkeypatch.setitem(main._scanner_config, "learning_epoch", epoch)
    _insert_closed(_floor_offset(1))   # after floor, before epoch -> excluded
    _insert_closed(kept)               # after epoch -> kept
    rows = main.db_get_all_closed_trades()
    assert len(rows) == 1
    assert rows[0]["ts"] == kept


def test_policy_defaults():
    cfg = main.DEFAULT_CONFIG
    # Trade with the tape, alert only A/B.
    assert cfg["counter_trend_allowed"] is False
    assert cfg["alert_min_grade"] == "B"
    assert 0 < cfg["max_breakout_extension"] <= 1.0
    # ORB stop widened to a full ORB range (was 0.5x).
    assert cfg["orb_stop_mult"] == 1.0
