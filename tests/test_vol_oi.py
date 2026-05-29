"""Tests for the volume-vs-open-interest confluence factor."""
import pytest

import oi_delta
import vol_oi


@pytest.fixture(autouse=True)
def _init_oi_db():
    # _init_db ran at import in the repo root; re-init in the test's tmp cwd.
    oi_delta._init_db()


def _seed(symbol, strike, expiry, opt_type, oi, volume):
    oi_delta.save_snapshot(symbol, [{
        "strike": strike, "expiry": expiry, "type": opt_type,
        "open_interest": oi, "volume": volume,
    }])


def test_unusual_when_volume_exceeds_oi():
    _seed("VOTST1", 500.0, "2026-06-05", "call", oi=1000, volume=2000)
    r = vol_oi.compute_vol_oi("VOTST1", 500.0, "2026-06-05", "call",
                              alpaca_volume=2000)
    assert r["ratio"] == 2.0
    assert r["flag"] == "UNUSUAL"
    assert r["points"] == vol_oi.UNUSUAL_PTS
    assert r["confirmed"] is True   # Databento OI + Alpaca volume both present


def test_elevated_band():
    _seed("VOTST2", 100.0, "2026-06-05", "put", oi=1000, volume=600)
    r = vol_oi.compute_vol_oi("VOTST2", 100.0, "2026-06-05", "put",
                              alpaca_volume=600)
    assert r["flag"] == "ELEVATED"
    assert r["points"] == vol_oi.ELEVATED_PTS


def test_normal_band_no_points():
    _seed("VOTST3", 100.0, "2026-06-05", "call", oi=1000, volume=200)
    r = vol_oi.compute_vol_oi("VOTST3", 100.0, "2026-06-05", "call",
                              alpaca_volume=200)
    assert r["flag"] == "NORMAL"
    assert r["points"] == 0


def test_missing_oi_is_unconfirmed():
    r = vol_oi.compute_vol_oi("NOPE", 100.0, "2026-06-05", "call",
                              alpaca_volume=5000, allow_fetch=False)
    assert r["confirmed"] is False
    assert r["flag"] is None
    assert r["points"] == 0


def test_volume_disagreement_softens_points():
    # Databento volume (stored) and Alpaca volume disagree by >2x -> softened.
    _seed("VOTST4", 100.0, "2026-06-05", "call", oi=1000, volume=300)
    r = vol_oi.compute_vol_oi("VOTST4", 100.0, "2026-06-05", "call",
                              alpaca_volume=3000)   # ratio 3.0 -> UNUSUAL
    assert r["flag"] == "UNUSUAL"
    assert r["volume_agree"] is False
    assert 0 < r["points"] < vol_oi.UNUSUAL_PTS
