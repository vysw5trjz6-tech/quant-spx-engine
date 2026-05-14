"""RV/IV gap signal + ATM-pair selector tests."""
import os
import sys
import sqlite3
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


@pytest.fixture
def fresh_iv_modules(tmp_path, monkeypatch):
    """Reload iv_rank with the IV cache pointed at a tmp dir."""
    monkeypatch.chdir(tmp_path)
    for m in ("iv_rank", "regime_filter", "iv_backfill"):
        if m in sys.modules:
            del sys.modules[m]
    import iv_rank
    return iv_rank


def _seed_iv(iv_rank_mod, symbol, ivs_by_date):
    """Insert (symbol, date, iv) triples into iv_history."""
    conn = sqlite3.connect(iv_rank_mod.IV_CACHE_DB)
    for d, iv in ivs_by_date:
        conn.execute("""
            INSERT OR REPLACE INTO iv_history (symbol, obs_date, atm_iv, updated_at)
            VALUES (?, ?, ?, datetime('now'))
        """, (symbol, d, iv))
    conn.commit()
    conn.close()


# =============================================
# get_rv_iv_gap
# =============================================

def test_gap_favorable_when_iv_well_above_rv(fresh_iv_modules, monkeypatch):
    iv_rank = fresh_iv_modules
    import regime_filter

    # IV = 25%, RV = 15% -> ratio 1.67 -> favorable
    _seed_iv(iv_rank, "AAPL", [("2026-05-13", 0.25)])
    monkeypatch.setattr(regime_filter, "get_realized_vol", lambda *a, **k: 15.0)

    gap = iv_rank.get_rv_iv_gap("AAPL")
    assert gap is not None
    assert gap["iv"] == 0.25
    assert gap["rv"] == 0.15
    assert gap["ratio"] == round(0.25 / 0.15, 3)
    assert gap["vrp_favorable"] is True


def test_gap_unfavorable_when_iv_close_to_rv(fresh_iv_modules, monkeypatch):
    iv_rank = fresh_iv_modules
    import regime_filter

    # IV = 18%, RV = 16% -> ratio 1.125 -> NOT favorable (< 1.20)
    _seed_iv(iv_rank, "TSLA", [("2026-05-13", 0.18)])
    monkeypatch.setattr(regime_filter, "get_realized_vol", lambda *a, **k: 16.0)

    gap = iv_rank.get_rv_iv_gap("TSLA")
    assert gap["vrp_favorable"] is False


def test_gap_returns_none_without_history(fresh_iv_modules, monkeypatch):
    iv_rank = fresh_iv_modules
    import regime_filter
    monkeypatch.setattr(regime_filter, "get_realized_vol", lambda *a, **k: 18.0)
    assert iv_rank.get_rv_iv_gap("NEVER_SEEN") is None


def test_gap_returns_none_when_rv_unavailable(fresh_iv_modules, monkeypatch):
    iv_rank = fresh_iv_modules
    import regime_filter
    _seed_iv(iv_rank, "MSFT", [("2026-05-13", 0.22)])
    monkeypatch.setattr(regime_filter, "get_realized_vol", lambda *a, **k: None)
    assert iv_rank.get_rv_iv_gap("MSFT") is None


# =============================================
# compute_iv_rank now folds in vrp fields
# =============================================

def test_compute_iv_rank_includes_vrp(fresh_iv_modules, monkeypatch):
    iv_rank = fresh_iv_modules
    import regime_filter

    # 60 days of synthetic IV history, today at high end of range
    base = datetime(2026, 3, 15).date()
    ivs = [(str(base - timedelta(days=i)), 0.10 + (i % 30) * 0.005)
           for i in range(60)]
    _seed_iv(iv_rank, "NVDA", ivs)
    monkeypatch.setattr(regime_filter, "get_realized_vol", lambda *a, **k: 12.0)

    out = iv_rank.compute_iv_rank("NVDA")
    assert out is not None
    assert "iv_rv_ratio" in out
    assert "vrp_favorable" in out
    assert out["rv"] == 0.12


# =============================================
# iv_backfill ATM-pair picker
# =============================================

def test_pick_atm_pair_picks_closest_to_spot():
    import iv_backfill

    rows = [
        # 30 DTE, multiple strikes
        {"date": "2024-01-15", "expiry": "2024-02-14", "strike": 95.0,
         "type": "call", "close": 6.0, "volume": 100, "dte": 30},
        {"date": "2024-01-15", "expiry": "2024-02-14", "strike": 95.0,
         "type": "put",  "close": 1.0, "volume": 100, "dte": 30},
        {"date": "2024-01-15", "expiry": "2024-02-14", "strike": 100.0,
         "type": "call", "close": 3.0, "volume": 100, "dte": 30},
        {"date": "2024-01-15", "expiry": "2024-02-14", "strike": 100.0,
         "type": "put",  "close": 3.0, "volume": 100, "dte": 30},
        {"date": "2024-01-15", "expiry": "2024-02-14", "strike": 110.0,
         "type": "call", "close": 1.0, "volume": 100, "dte": 30},
        {"date": "2024-01-15", "expiry": "2024-02-14", "strike": 110.0,
         "type": "put",  "close": 8.0, "volume": 100, "dte": 30},
    ]
    call, put = iv_backfill._pick_atm_pair(rows, spot=99.0)
    assert call is not None and put is not None
    assert call["strike"] == 100.0
    assert put["strike"] == 100.0


def test_pick_atm_pair_prefers_30_dte_when_multiple_expiries():
    import iv_backfill
    rows = [
        # Same strike, two expiries
        {"date": "2024-01-15", "expiry": "2024-02-01", "strike": 100.0,
         "type": "call", "close": 2.0, "volume": 0, "dte": 17},
        {"date": "2024-01-15", "expiry": "2024-02-01", "strike": 100.0,
         "type": "put",  "close": 2.0, "volume": 0, "dte": 17},
        {"date": "2024-01-15", "expiry": "2024-02-14", "strike": 100.0,
         "type": "call", "close": 3.0, "volume": 0, "dte": 30},
        {"date": "2024-01-15", "expiry": "2024-02-14", "strike": 100.0,
         "type": "put",  "close": 3.0, "volume": 0, "dte": 30},
    ]
    # 17 DTE is below DTE_MIN=21; 30 DTE wins
    call, put = iv_backfill._pick_atm_pair(rows, spot=100.0)
    # The picker doesn't enforce DTE_MIN itself (databento_adapter does);
    # given both available, prefer the one closest to DTE_TARGET=30
    assert call["dte"] == 30
    assert put["dte"] == 30


def test_pick_atm_pair_returns_none_when_no_matched_pair():
    import iv_backfill
    rows = [
        {"date": "2024-01-15", "expiry": "2024-02-14", "strike": 100.0,
         "type": "call", "close": 3.0, "volume": 0, "dte": 30},
        # No matching put at the same strike
        {"date": "2024-01-15", "expiry": "2024-02-14", "strike": 105.0,
         "type": "put",  "close": 5.0, "volume": 0, "dte": 30},
    ]
    call, put = iv_backfill._pick_atm_pair(rows, spot=100.0)
    assert call is None and put is None
