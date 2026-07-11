# Data-efficiency hardening:
#   1. Module SQLite stores resolve onto persistent storage (DATA_DIR /
#      Railway volume), not the ephemeral container working dir.
#   2. options_flow no longer buys the tcbbo quote stream it never read.
#   3. OI-only chain requests reuse a cached price-enriched chain instead of
#      re-paying Databento for the identical statistics pull.
#   4. Retention sweeps: databento_cache.db and oi_history.db no longer grow
#      unboundedly.

import inspect
import os
import sys
from datetime import date, timedelta

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import db_utils
import databento_adapter as da
import oi_delta
import options_flow


# ---------------------------------------------------------------------------
# 1. Persistent path resolution
# ---------------------------------------------------------------------------

def test_data_path_resolution(monkeypatch):
    monkeypatch.setenv("DATA_DIR", "/some/dir/")
    assert db_utils.data_path("x.db") == "/some/dir/x.db"

    monkeypatch.delenv("DATA_DIR")
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", "/vol")
    assert db_utils.data_path("x.db") == "/vol/x.db"

    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH")
    assert db_utils.data_path("x.db") == "/tmp/x.db"


def test_module_db_paths_are_absolute():
    """A bare relative DB filename lives in the ephemeral working dir and is
    wiped on every redeploy. Every module store must resolve absolutely."""
    import iv_rank
    import market_profile
    import volume_truth

    for path in (
        iv_rank.IV_CACHE_DB,
        oi_delta.OI_DB,
        volume_truth.VOL_CACHE_DB,
        market_profile.PROFILE_DB,
        options_flow.FLOW_DB,
        da._DB_CACHE,
    ):
        assert os.path.isabs(path), path


def test_storage_status_flags_ephemeral(monkeypatch):
    """Storage health-check must report ephemeral/unwritable loudly so a
    missing Railway volume can't silently wipe accumulated data each boot."""
    import importlib
    import main

    # No DATA_DIR / volume -> ephemeral /tmp fallback.
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    monkeypatch.setattr(main, "_data_dir", "/tmp")
    st = main._storage_status()
    assert st["persistent"] is False
    assert st["source"] == "/tmp fallback"
    assert "ephemeral" in st["detail"]

    # A mounted Railway volume -> persistent + writable.
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", _TMP_VOL)
    monkeypatch.setattr(main, "_data_dir", _TMP_VOL)
    st = main._storage_status()
    assert st["persistent"] is True
    assert st["writable"] is True
    assert st["source"] == "RAILWAY_VOLUME_MOUNT_PATH"


import tempfile as _tempfile
_TMP_VOL = _tempfile.mkdtemp(prefix="qspx-vol-")


# ---------------------------------------------------------------------------
# 2. No paid-but-unread quote pull in options_flow
# ---------------------------------------------------------------------------

def test_summarize_trades_takes_no_quotes_param():
    params = inspect.signature(options_flow._summarize_trades).parameters
    assert "quotes_df" not in params


# ---------------------------------------------------------------------------
# 3. OI-only chain requests reuse the price-enriched cache
# ---------------------------------------------------------------------------

def test_chain_oi_request_served_from_px_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(da, "_DB_CACHE", str(tmp_path / "dbn_cache.db"))
    da._init_cache()

    target = date(2026, 6, 12)
    chain = [{"strike": 600.0, "expiry": "2026-06-19", "type": "call",
              "open_interest": 10, "volume": 5, "price": 1.25,
              "implied_volatility": None}]
    da._cache_set("chain_px_SPY_{}".format(target.isoformat()),
                  {"chain": chain})

    # Truthy client so the function reaches the cache checks; if the cache
    # misses, the dummy client would blow up on .timeseries -- which is the
    # point: no network/billing path may be reached.
    monkeypatch.setattr(da, "_get_client", lambda: object())

    out = da.get_options_chain_snapshot("SPY", target_date_et=target,
                                        with_price=False)
    assert out == chain


# ---------------------------------------------------------------------------
# 4. Retention sweeps
# ---------------------------------------------------------------------------

def test_databento_cache_prunes_expired_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(da, "_DB_CACHE", str(tmp_path / "dbn_cache.db"))
    da._init_cache()

    conn = db_utils.connect(da._DB_CACHE)
    old = (da._utcnow()
           - timedelta(seconds=da._CACHE_RETENTION_SECS + 3600)).isoformat()
    conn.execute("INSERT INTO cache (key, value, stored_at) VALUES (?, ?, ?)",
                 ("chain_SPY_2026-01-01", "{}", old))
    conn.execute("INSERT INTO cache (key, value, stored_at) VALUES (?, ?, ?)",
                 ("chain_SPY_recent", "{}", da._utcnow().isoformat()))
    conn.commit()
    conn.close()

    da._init_cache()  # startup prune

    conn = db_utils.connect(da._DB_CACHE)
    keys = [r[0] for r in conn.execute("SELECT key FROM cache").fetchall()]
    conn.close()
    assert keys == ["chain_SPY_recent"]


def test_oi_snapshots_prune_beyond_retention(monkeypatch, tmp_path):
    monkeypatch.setattr(oi_delta, "OI_DB", str(tmp_path / "oi.db"))
    oi_delta._init_db()
    monkeypatch.setattr(oi_delta, "_last_prune_ymd", None)

    chain = [{"strike": 600.0, "expiry": "2026-06-19", "type": "call",
              "open_interest": 100}]
    assert oi_delta.save_snapshot("SPY", chain, snap_date="2026-01-02") == 1
    assert oi_delta.save_snapshot("SPY", chain, snap_date="2026-06-12") == 1

    conn = db_utils.connect(oi_delta.OI_DB)
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT snap_date FROM oi_snapshots ORDER BY snap_date"
    ).fetchall()]
    conn.close()
    # 2026-01-02 is more than OI_RETENTION_DAYS before the newest snapshot.
    assert dates == ["2026-06-12"]


# ---------------------------------------------------------------------------
# 5. IV backfill cost guard
# ---------------------------------------------------------------------------

@pytest.fixture
def guarded_backfill(monkeypatch):
    """iv_backfill wired to a fake Databento that must never be pulled."""
    import iv_backfill

    monkeypatch.setattr(da, "is_available", lambda: True)
    monkeypatch.setattr(iv_backfill, "_alpaca_daily_closes",
                        lambda sym, s, e: {"2026-06-11": 600.0})
    monkeypatch.setattr(iv_backfill, "_est_spend", 0.0)

    def _no_pull(*a, **k):
        raise AssertionError("paid OPRA pull issued despite cost guard")
    monkeypatch.setattr(da, "get_historical_daily_options", _no_pull)
    return iv_backfill


def test_backfill_skips_symbol_over_per_symbol_cap(guarded_backfill, monkeypatch):
    # 5.0 per schema x 2 schemas = $10 estimate > $2/symbol default cap.
    monkeypatch.setattr(da, "get_cost_estimate", lambda *a, **k: 5.0)
    res = guarded_backfill.backfill_symbol("AAPL")
    assert res["rows"] == 0
    assert res["skip_reason"] == "budget"


def test_backfill_skips_on_estimate_failure(guarded_backfill, monkeypatch):
    monkeypatch.setattr(da, "get_cost_estimate",
                        lambda *a, **k: {"error": "boom"})
    res = guarded_backfill.backfill_symbol("AAPL")
    assert res["rows"] == 0
    assert res["skip_reason"] == "estimate_failed"


def test_backfill_total_budget_stops_run(guarded_backfill, monkeypatch):
    iv_backfill = guarded_backfill
    monkeypatch.setattr(iv_backfill, "MAX_PER_SYMBOL_USD", 50.0)
    monkeypatch.setattr(iv_backfill, "MAX_TOTAL_USD", 5.0)
    # $2 per schema -> $4/symbol: first fits the $5 total, second must not.
    monkeypatch.setattr(da, "get_cost_estimate", lambda *a, **k: 2.0)
    monkeypatch.setattr(da, "get_historical_daily_options",
                        lambda *a, **k: [])

    first = iv_backfill.backfill_symbol("AAPL")
    assert first["skip_reason"] == "no_data"      # paid, pull came back empty

    second = iv_backfill.backfill_symbol("NVDA")  # $4 + $4 > $5 cap
    assert second["rows"] == 0
    assert second["skip_reason"] == "budget"


def test_oi_prune_keeps_recent_history(monkeypatch, tmp_path):
    monkeypatch.setattr(oi_delta, "OI_DB", str(tmp_path / "oi.db"))
    oi_delta._init_db()
    monkeypatch.setattr(oi_delta, "_last_prune_ymd", None)

    chain = [{"strike": 600.0, "expiry": "2026-06-19", "type": "call",
              "open_interest": 100}]
    oi_delta.save_snapshot("SPY", chain, snap_date="2026-06-11")
    oi_delta.save_snapshot("SPY", chain, snap_date="2026-06-12")

    # Yesterday's snapshot must survive -- compute_delta diffs against it.
    delta = oi_delta.compute_delta("SPY", current_date="2026-06-12")
    assert delta is not None
    assert delta["prior_date"] == "2026-06-11"
