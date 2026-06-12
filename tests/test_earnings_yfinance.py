"""Smoke tests for the yfinance-based earnings calendar.

We don't hit Yahoo over the network. Instead we monkeypatch
yf.Ticker(symbol).earnings_dates with a synthetic DataFrame and verify
the safety_gates round-trip:
  - update_earnings_calendar() inserts rows with bmo/amc heuristic
  - days_to_earnings() picks the next future date
  - earnings_filter() blocks swing trades inside the window
"""
import os
import sys
import sqlite3
import types
from datetime import datetime, timedelta

import pytest
import pytz


_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture
def fresh_safety_gates(tmp_path, monkeypatch):
    """Re-import safety_gates with EARNINGS_CACHE_DB pointed into tmp_path."""
    monkeypatch.chdir(tmp_path)
    # EARNINGS_CACHE_DB resolves via db_utils.data_path (DATA_DIR) at import
    # time, so the env var -- not the cwd -- decides where the DB lands.
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    if "safety_gates" in sys.modules:
        del sys.modules["safety_gates"]
    import safety_gates  # noqa: WPS433
    return safety_gates


def _stub_yfinance(monkeypatch, df):
    """Install a fake yfinance module whose Ticker(...).earnings_dates == df."""
    fake = types.ModuleType("yfinance")

    class _T:
        def __init__(self, _sym):
            self.earnings_dates = df

    fake.Ticker = _T
    monkeypatch.setitem(sys.modules, "yfinance", fake)


class _FakeDF:
    """Minimal stand-in for pandas DataFrame used by update_earnings_calendar.

    Only needs to support len(df) and iteration over df.index. Each index
    element behaves like a pandas Timestamp (has .strftime and .hour).
    """
    def __init__(self, datetimes):
        self.index = list(datetimes)

    def __len__(self):
        return len(self.index)


def _df_with_dates(dates_with_hours):
    et = pytz.timezone("America/New_York")
    return _FakeDF([et.localize(datetime(*dt)) for dt in dates_with_hours])


def _empty_df():
    return _FakeDF([])


# =============================================
# update_earnings_calendar
# =============================================

def test_update_earnings_writes_rows(fresh_safety_gates, monkeypatch):
    sg = fresh_safety_gates
    df = _df_with_dates([
        (2026, 5, 20,  8, 30),   # BMO
        (2026, 8, 15, 16,  0),   # AMC
        (2026, 2, 10, 12,  0),   # historical, ambiguous time
    ])
    _stub_yfinance(monkeypatch, df)

    assert sg.update_earnings_calendar("AAPL") is True

    conn = sqlite3.connect(sg.EARNINGS_CACHE_DB)
    rows = conn.execute(
        "SELECT report_date, time_of_day FROM earnings WHERE symbol=? "
        "ORDER BY report_date", ("AAPL",)
    ).fetchall()
    conn.close()

    dates = sorted(r[0] for r in rows)
    assert dates == ["2026-02-10", "2026-05-20", "2026-08-15"]
    by_date = {r[0]: r[1] for r in rows}
    assert by_date["2026-05-20"] == "bmo"
    assert by_date["2026-08-15"] == "amc"


def test_update_returns_false_when_yfinance_missing(fresh_safety_gates, monkeypatch):
    sg = fresh_safety_gates
    monkeypatch.setitem(sys.modules, "yfinance", None)
    assert sg.update_earnings_calendar("AAPL") is False


def test_update_returns_false_on_empty_df(fresh_safety_gates, monkeypatch):
    sg = fresh_safety_gates
    _stub_yfinance(monkeypatch, _empty_df())
    assert sg.update_earnings_calendar("AAPL") is False


# =============================================
# end-to-end earnings_filter behavior
# =============================================

def test_earnings_filter_blocks_swing_inside_window(fresh_safety_gates, monkeypatch):
    sg = fresh_safety_gates
    et = pytz.timezone("America/New_York")
    today = datetime.now(et).date()
    soon  = today + timedelta(days=3)
    far   = today + timedelta(days=45)
    df = _df_with_dates([
        (soon.year, soon.month, soon.day,  8, 30),
        (far.year,  far.month,  far.day,  16,  0),
    ])
    _stub_yfinance(monkeypatch, df)
    assert sg.update_earnings_calendar("AAPL") is True

    allowed_swing, reason_swing = sg.earnings_filter("AAPL", "swing")
    assert allowed_swing is False
    assert "earnings_in_3_days" in reason_swing or "earnings_in_2_days" in reason_swing

    allowed_dte, reason_dte = sg.earnings_filter("AAPL", "0dte")
    assert allowed_dte is True   # 0DTE inside window is allowed but flagged
