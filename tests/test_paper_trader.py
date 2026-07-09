"""Tests for the EOD paper trader.

The unit under test is walk_bars(), which determines a synthetic trade's
outcome from the 5-min bars that fall AFTER the signal's entry bar.

Convention recap:
  - 'CALL': stop_under < entry_under < target_under
  - 'PUT' : target_under < entry_under < stop_under
  - Both stop and target touched in the same bar = LOSS (conservative)
  - Neither touched = EOD with signed R-multiple
"""
from datetime import datetime
import os
import sqlite3

import paper_trader as pt


def _bar(high, low, close=None):
    return {"o": low, "h": high, "l": low, "c": close if close is not None else (high + low) / 2}


# =============================================
# walk_bars()
# =============================================

def test_call_target_hit_returns_win_with_correct_r_multiple():
    # Entry 100, stop 99, target 102 -> risk 1, reward 2 -> WIN +2R
    bars = [_bar(101.5, 99.5), _bar(102.5, 101.0)]
    outcome = pt.walk_bars(bars, "CALL", entry_under=100.0,
                            stop_under=99.0, target_under=102.0)
    assert outcome == ("WIN", 102.0, 2.0)


def test_call_stop_hit_returns_loss_minus_one_r():
    bars = [_bar(100.5, 99.8), _bar(99.5, 98.5)]
    outcome = pt.walk_bars(bars, "CALL", entry_under=100.0,
                            stop_under=99.0, target_under=102.0)
    assert outcome == ("LOSS", 99.0, -1.0)


def test_call_stop_and_target_in_same_bar_is_loss():
    # Whipsaw bar that hits both -- conservative convention: stop first
    bars = [_bar(102.5, 98.5)]
    outcome = pt.walk_bars(bars, "CALL", entry_under=100.0,
                            stop_under=99.0, target_under=102.0)
    assert outcome == ("LOSS", 99.0, -1.0)


def test_call_no_hit_returns_eod_with_signed_r():
    # Closes at 100.5 -> half a risk-unit favorable -> +0.5R
    bars = [_bar(101.0, 99.5), _bar(101.0, 99.8, close=100.5)]
    outcome = pt.walk_bars(bars, "CALL", entry_under=100.0,
                            stop_under=99.0, target_under=102.0)
    assert outcome[0] == "EOD"
    assert outcome[1] == 100.5
    assert outcome[2] == 0.5


def test_call_no_hit_eod_can_be_negative():
    # Drifted lower without hitting stop -> negative R
    bars = [_bar(100.5, 99.5, close=99.3)]
    outcome = pt.walk_bars(bars, "CALL", entry_under=100.0,
                            stop_under=99.0, target_under=102.0)
    assert outcome[0] == "EOD"
    assert outcome[2] == -0.7


# PUT mirror tests

def test_put_target_hit_returns_win():
    # Entry 100, stop 101, target 98 -> risk 1, reward 2 -> WIN +2R
    bars = [_bar(100.5, 99.5), _bar(99.5, 97.5)]
    outcome = pt.walk_bars(bars, "PUT", entry_under=100.0,
                            stop_under=101.0, target_under=98.0)
    assert outcome == ("WIN", 98.0, 2.0)


def test_put_stop_hit_returns_loss():
    bars = [_bar(101.5, 100.2)]
    outcome = pt.walk_bars(bars, "PUT", entry_under=100.0,
                            stop_under=101.0, target_under=98.0)
    assert outcome == ("LOSS", 101.0, -1.0)


def test_put_eod_signed_r():
    bars = [_bar(100.5, 99.0, close=99.0)]
    outcome = pt.walk_bars(bars, "PUT", entry_under=100.0,
                            stop_under=101.0, target_under=98.0)
    assert outcome[0] == "EOD"
    # Closed 1.0 below entry -> favorable by 1R
    assert outcome[2] == 1.0


# Guards

def test_returns_none_on_bad_inputs():
    bars = [_bar(101, 99)]
    assert pt.walk_bars(bars, "LONG", 100, 99, 102) is None
    assert pt.walk_bars(bars, "CALL", None, 99, 102) is None
    assert pt.walk_bars(bars, "CALL", 100, None, 102) is None
    assert pt.walk_bars(bars, "CALL", 100, 99, None) is None
    # zero-risk
    assert pt.walk_bars(bars, "CALL", 100, 100, 102) is None
    # empty bars
    assert pt.walk_bars([], "CALL", 100, 99, 102) is None


# =============================================
# init_paper_columns() schema migration
# =============================================

def test_init_paper_columns_is_idempotent(tmp_path):
    db_path = str(tmp_path / "trades.db")
    conn = sqlite3.connect(db_path)
    # Seed minimal tables matching main.py's init_db
    conn.execute("""CREATE TABLE signals (
        id INTEGER PRIMARY KEY, ts TEXT, symbol TEXT, direction TEXT,
        price REAL, score REAL, premium REAL, strike TEXT, contracts INTEGER,
        stop REAL, target REAL
    )""")
    conn.execute("""CREATE TABLE trades (
        id INTEGER PRIMARY KEY, ts TEXT, symbol TEXT, direction TEXT,
        premium REAL, contracts INTEGER, stop REAL, target REAL,
        outcome TEXT, exit_price REAL, pnl REAL, r_mult REAL,
        grade TEXT, grade_pts INTEGER, gap_pct REAL, gap_dir TEXT,
        rs REAL, entry_hour REAL, entry_under REAL, signal_type TEXT
    )""")
    conn.commit()
    conn.close()

    pt.init_paper_columns(db_path)
    # Idempotent: a second call must not raise
    pt.init_paper_columns(db_path)

    conn = sqlite3.connect(db_path)
    sig_cols = {row[1] for row in conn.execute("PRAGMA table_info(signals)")}
    trade_cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
    conn.close()

    for col in ("entry_under", "und_stop", "und_target_t1", "und_target_t2",
                "signal_type", "grade", "grade_pts"):
        assert col in sig_cols, col
    assert "mode" in trade_cols


# =============================================
# parse_iso edge cases (used to filter bars after signal time)
# =============================================

def test_parse_iso_handles_z_suffix():
    dt = pt._parse_iso("2026-05-13T14:30:00Z")
    assert dt is not None
    assert dt.year == 2026 and dt.hour == 14


def test_parse_iso_returns_none_on_garbage():
    assert pt._parse_iso(None)        is None
    assert pt._parse_iso("")          is None
    assert pt._parse_iso("not-a-date") is None


# =============================================
# AI-label propagation: entry_hour + horizon on paper rows
# =============================================

def test_entry_hour_et_converts_utc_to_fractional_et_hour():
    # 14:35 UTC in May (EDT, UTC-4) -> 10:35 ET -> 10.58
    assert pt._entry_hour_et("2026-05-13T14:35:00Z") == 10.58
    # 15:00 UTC in January (EST, UTC-5) -> 10:00 ET
    assert pt._entry_hour_et("2026-01-13T15:00:00+00:00") == 10.0


def test_entry_hour_et_none_on_missing_or_naive_ts():
    assert pt._entry_hour_et(None) is None
    assert pt._entry_hour_et("garbage") is None
    # Naive timestamp (no tz) can't be converted safely
    assert pt._entry_hour_et("2026-05-13T14:35:00") is None


def test_paper_rows_carry_entry_hour_horizon_and_grade(tmp_path, monkeypatch):
    """Paper trades must inherit the signal's grade/horizon and a real
    entry_hour. NULLs here poisoned the AI stats: entry_hour defaulted to
    the 9:30-10:00 bucket and ungraded weekly rows became the '?' grade."""
    import pytz as _pytz
    from datetime import datetime as _dt

    db_path = str(tmp_path / "trades.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE signals (
        id INTEGER PRIMARY KEY, ts TEXT, symbol TEXT, direction TEXT,
        price REAL, score REAL, premium REAL, strike TEXT, contracts INTEGER,
        stop REAL, target REAL
    )""")
    conn.execute("""CREATE TABLE trades (
        id INTEGER PRIMARY KEY, ts TEXT, symbol TEXT, direction TEXT,
        premium REAL, contracts INTEGER, stop REAL, target REAL,
        outcome TEXT, exit_price REAL, pnl REAL, r_mult REAL,
        grade TEXT, grade_pts INTEGER, gap_pct REAL, gap_dir TEXT,
        rs REAL, entry_hour REAL, entry_under REAL, signal_type TEXT
    )""")
    conn.commit()
    conn.close()
    pt.init_paper_columns(db_path)

    # Signal stamped today (ET date) so _todays_signals picks it up.
    et_today = _dt.now(_pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
    sig_ts   = "{}T15:35:00+00:00".format(et_today)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT INTO signals (ts, symbol, direction, price, entry_under,
                             und_stop, und_target_t1, und_target_t2,
                             signal_type, grade, grade_pts, horizon)
        VALUES (?, 'GOOGL', 'CALL', 100.0, 100.0, 99.0, 102.0, 104.0,
                'WYCKOFF_SPRING', 'B', 72, 'WEEKLY')
    """, (sig_ts,))
    conn.commit()
    conn.close()

    # One bar after the signal: hits T1 (102) but not T2 (104) or stop.
    bar_ts = "{}T16:00:00+00:00".format(et_today)
    monkeypatch.setattr(pt.data_fetcher, "get_intraday",
                        lambda sym: [{"t": bar_ts, "o": 100.5, "h": 103.0,
                                      "l": 99.5, "c": 102.5}])

    inserted, skipped = pt.run_paper_trader(db_path)
    assert inserted == 2   # T1 row + T2 row

    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT signal_type, outcome, grade, grade_pts, horizon, entry_hour, mode
        FROM trades ORDER BY id
    """).fetchall()
    conn.close()

    assert len(rows) == 2
    outcomes = {r[0].rsplit(":", 1)[-1]: r for r in rows}
    assert outcomes["T1"][1] == "WIN"
    assert outcomes["T2"][1] == "EOD"
    for r in rows:
        assert r[2] == "B"          # grade propagated, not NULL/'?'
        assert r[3] == 72
        assert r[4] == "WEEKLY"     # horizon propagated
        assert r[5] is not None     # entry_hour stamped from signal ts
        assert 10.0 <= r[5] <= 12.0  # 15:35 UTC is 10:35/11:35 ET (DST-dep.)
        assert r[6] == "paper"
