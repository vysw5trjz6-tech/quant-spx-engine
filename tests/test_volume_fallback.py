# Tests for the data-efficiency / signal-quality hardening:
#   1. The volume fallback uses a U-shaped intraday distribution instead of a
#      monotonic power law, so a bar's ratio is judged against the real
#      open/lunch/close volume shape.
#   2. A universe-wide feed mismatch is surfaced (volume_health) and trips a
#      one-shot auto-rebuild past a threshold.
#   3. Live ATM-IV consumers reuse the daily snapshot (iv_rank.get_recent_iv)
#      instead of re-probing the options endpoint every scan.

import os
import sys
from datetime import datetime, timedelta

import pytz

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import db_utils
import iv_rank
import main


# ---------------------------------------------------------------------------
# 1. U-shaped intraday volume distribution
# ---------------------------------------------------------------------------

def test_vol_weights_form_a_u_shape_and_normalize():
    ws = main._INTRADAY_VOL_WEIGHTS
    assert len(ws) == main._RTH_BARS
    assert abs(sum(ws) - 1.0) < 1e-9
    # Open is the heaviest slot, close second, midday the trough.
    assert ws[0] > ws[77] > ws[39]
    # The opening drive should dwarf the lunch lull by a wide margin.
    assert ws[0] / ws[39] > 2.0


def test_expected_fraction_clamps_out_of_range_index():
    assert main._expected_bar_vol_fraction(-5) == main._INTRADAY_VOL_WEIGHTS[0]
    assert main._expected_bar_vol_fraction(999) == main._INTRADAY_VOL_WEIGHTS[-1]


def test_legacy_fallback_ratio_respects_intraday_shape():
    """The same raw bar volume is 'average' at lunch but 'light' at the open --
    a monotonic power law could not tell these apart."""
    avg_daily_vol = 78000.0
    daily = [{"o": 1, "h": 1, "l": 1, "c": 1, "v": avg_daily_vol} for _ in range(10)]

    lunch_idx = 39
    lunch_expected = avg_daily_vol * main._expected_bar_vol_fraction(lunch_idx)
    intraday = [{"v": 1} for _ in range(lunch_idx + 1)]
    intraday[lunch_idx]["v"] = lunch_expected  # exactly average for the slot

    # symbol=None forces the legacy fallback path (skips volume_truth lookup).
    ratio, label = main.get_time_vol_ratio(intraday, daily, lunch_idx, symbol=None)
    assert abs(ratio - 1.0) < 0.05
    assert label != "N/A"

    # That same volume at the open is well below the slot's expectation.
    open_intraday = [{"v": lunch_expected}]
    open_ratio, _ = main.get_time_vol_ratio(open_intraday, daily, 0, symbol=None)
    assert open_ratio < ratio


# ---------------------------------------------------------------------------
# 2. Feed-mismatch health + auto-rebuild
# ---------------------------------------------------------------------------

def _reset_suspect_state():
    with main._tv_suspect_lock:
        main._tv_suspect_today["date"] = None
        main._tv_suspect_today["symbols"] = set()
        main._tv_suspect_today["rebuilt"] = False


def test_suspect_health_counts_distinct_symbols(monkeypatch):
    _reset_suspect_state()
    # Don't let the threshold spawn a real rebuild thread (network).
    monkeypatch.setattr(main, "HAS_VOLUME_TRUTH", False)

    for sym in ("AAA", "BBB", "AAA"):
        main._note_tv_suspect(sym)
    h = main.volume_health()
    assert h["suspect_count"] == 2
    assert h["suspect_symbols"] == ["AAA", "BBB"]
    assert h["auto_rebuilt"] is False


def test_suspect_threshold_trips_single_rebuild(monkeypatch):
    _reset_suspect_state()
    monkeypatch.setattr(main, "HAS_VOLUME_TRUTH", False)

    n = main._TV_SUSPECT_REBUILD_THRESHOLD
    for i in range(n):
        main._note_tv_suspect("S{}".format(i))
    h = main.volume_health()
    assert h["suspect_count"] == n
    assert h["auto_rebuilt"] is True


# ---------------------------------------------------------------------------
# 3. ATM IV reuse from the daily snapshot
# ---------------------------------------------------------------------------

def _write_iv(symbol, obs_date, iv):
    conn = db_utils.connect(iv_rank.IV_CACHE_DB)
    conn.execute(
        "INSERT OR REPLACE INTO iv_history (symbol, obs_date, atm_iv, updated_at)"
        " VALUES (?, ?, ?, ?)",
        (symbol, obs_date, iv, datetime.now(pytz.utc).isoformat()))
    conn.commit()
    conn.close()


def _today_et():
    return datetime.now(pytz.timezone("America/New_York")).date()


def test_get_recent_iv_returns_fresh_snapshot():
    sym = "RCNT"
    _write_iv(sym, _today_et().strftime("%Y-%m-%d"), 0.2234)
    assert iv_rank.get_recent_iv(sym) == 0.2234


def test_get_recent_iv_rejects_stale_snapshot():
    sym = "STALE"
    old = (_today_et() - timedelta(days=10)).strftime("%Y-%m-%d")
    _write_iv(sym, old, 0.31)
    assert iv_rank.get_recent_iv(sym) is None


def test_get_recent_iv_none_when_missing():
    assert iv_rank.get_recent_iv("NOPE_SYM") is None


def test_key_levels_prefers_snapshot_over_live_fetch(monkeypatch):
    import key_levels
    sym = "KLVL"
    _write_iv(sym, _today_et().strftime("%Y-%m-%d"), 0.18)

    def _boom(*a, **k):
        raise AssertionError("live fetch_atm_iv called despite a fresh snapshot")
    monkeypatch.setattr(iv_rank, "fetch_atm_iv", _boom)

    key_levels.clear_cache()
    daily = [{"t": i, "o": 100, "h": 101, "l": 99, "c": 100, "v": 1}
             for i in range(20)]
    kl = key_levels.get_key_levels(sym, daily_bars=daily, spot=100.0,
                                   direction="CALL", week_expiry="2026-06-26",
                                   dte=8)
    assert kl.atm_iv == 0.18
