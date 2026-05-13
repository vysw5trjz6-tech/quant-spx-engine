import math

import regime_filter as rf


def test_compressed_no_longer_hard_disables_trend_strats():
    """Regression test for the SPY 2026-05-13 miss: COMPRESSED used to set
    orb=False / vwap_trend=False / ib_extension=False, which gated us out of
    a clean trend day. After the fix these stay enabled at 0.5x size with a
    score penalty instead."""
    rules = rf.REGIME_STRATEGY_RULES["COMPRESSED"]
    assert rules["orb"]            is True
    assert rules["vwap_trend"]     is True
    assert rules["vwap_mr"]        is True
    assert rules["ib_extension"]   is True
    # Half size still applies; bleed in low IV is real
    assert rules["size_multiplier"] == 0.5
    # New: raises the bar for trend signals to fire
    assert rules.get("score_penalty_trend", 0) >= 10


def test_rolling_rv20_produces_expected_count():
    # 50 daily closes -> 50-21+1 = 30 rolling 20-day stdevs
    closes = [100 * math.exp(0.001 * i) for i in range(50)]
    series = rf._rolling_rv20(closes)
    assert len(series) == 30
    # All RVs from a smooth exponential should be near zero
    for v in series:
        assert v < 5.0  # well under 5% annualized


def test_check_expansion_watch_requires_all_three_conditions():
    # Happy path: low RV, tight gap, contango
    assert rf.check_expansion_watch(15.0, 0.30, "CONTANGO") is True
    assert rf.check_expansion_watch(5.0,  0.10, "FLAT")     is True

    # Reject if RV percentile too high (above bottom quintile)
    assert rf.check_expansion_watch(35.0, 0.30, "CONTANGO") is False

    # Reject if gap too wide (already a directional decision overnight)
    assert rf.check_expansion_watch(10.0, 0.80, "CONTANGO") is False

    # Reject if term structure is backwardated (real stress, not a squeeze)
    assert rf.check_expansion_watch(10.0, 0.30, "BACKWARDATION") is False
    assert rf.check_expansion_watch(10.0, 0.30, "DEEP_BACKWARDATION") is False

    # Reject on missing inputs
    assert rf.check_expansion_watch(None, 0.30, "CONTANGO") is False
    assert rf.check_expansion_watch(10.0, None, "CONTANGO") is False


def test_apply_expansion_override_only_acts_on_compressed(monkeypatch):
    # Force-mock the helpers so this is hermetic (no network)
    monkeypatch.setattr(rf, "get_rv20_percentile", lambda symbol="SPY": (9.8, 15.0))
    monkeypatch.setattr(rf, "get_vix_term_structure", lambda: {"label": "FLAT", "ratio": 1.0})

    # COMPRESSED with squeeze conditions -> flips to expansion_watch
    regime = {
        "regime":   "COMPRESSED",
        "rules":    dict(rf.REGIME_STRATEGY_RULES["COMPRESSED"]),
        "note":     "",
    }
    out = rf.apply_expansion_override(regime, gap_pct_abs=0.25, symbol="SPY")
    assert out["expansion_watch"] is True
    # Rules now match LOW_VOL (trend strats fully on, size 0.85)
    assert out["rules"]["orb"]              is True
    assert out["rules"]["vwap_trend"]       is True
    assert out["rules"]["size_multiplier"]  == rf.REGIME_STRATEGY_RULES["LOW_VOL"]["size_multiplier"]
    # Score penalty should not survive into LOW_VOL rules
    assert "score_penalty_trend" not in out["rules"]
    assert "EXPANSION_WATCH" in out["note"]

    # NORMAL regime is left alone even with squeeze conditions
    regime2 = {
        "regime": "NORMAL",
        "rules":  dict(rf.REGIME_STRATEGY_RULES["NORMAL"]),
        "note":   "",
    }
    out2 = rf.apply_expansion_override(regime2, gap_pct_abs=0.25, symbol="SPY")
    assert out2["expansion_watch"] is False
    assert out2["rules"] == rf.REGIME_STRATEGY_RULES["NORMAL"]


def test_apply_expansion_override_no_op_when_conditions_fail(monkeypatch):
    # RV in middle of range -> no override even if regime is COMPRESSED
    monkeypatch.setattr(rf, "get_rv20_percentile", lambda symbol="SPY": (13.0, 45.0))
    monkeypatch.setattr(rf, "get_vix_term_structure", lambda: {"label": "CONTANGO"})

    regime = {
        "regime":   "COMPRESSED",
        "rules":    dict(rf.REGIME_STRATEGY_RULES["COMPRESSED"]),
        "note":     "baseline",
    }
    out = rf.apply_expansion_override(regime, gap_pct_abs=0.25, symbol="SPY")
    assert out["expansion_watch"] is False
    # Rules unchanged; original COMPRESSED penalty intact
    assert out["rules"]["score_penalty_trend"] == \
           rf.REGIME_STRATEGY_RULES["COMPRESSED"]["score_penalty_trend"]


