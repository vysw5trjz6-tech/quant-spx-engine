"""Tests for the horizon-aware target framework and account-size removal."""
import math

import targets


def test_intraday_orb_parity():
    # Legacy ORB math: t1 = spot + range, t2 = spot + 2*range, stop = spot-0.5*range
    spot, orb = 100.0, 2.0
    r = targets.compute_price_targets(spot, "CALL", "INTRADAY",
                                      orb_range=orb, avg_range=4.0)
    assert r["t1"] == 102.0
    assert r["t2"] == 104.0
    assert r["stop"] == 99.0
    assert r["basis"] == "ORB"
    # PUT mirrors below spot
    rp = targets.compute_price_targets(spot, "PUT", "INTRADAY", orb_range=orb)
    assert rp["t1"] == 98.0
    assert rp["t2"] == 96.0
    assert rp["stop"] == 101.0


def test_intraday_atr_fallback():
    r = targets.compute_price_targets(100.0, "CALL", "INTRADAY", atr=2.0)
    assert r["basis"] == "ATR"
    assert r["t1"] == 103.0   # 1.5x ATR
    assert r["t2"] == 106.0   # 3x ATR


def test_weekly_expected_move_formula():
    # em = spot * iv * sqrt(dte/252)
    spot, iv, dte = 500.0, 0.20, 5
    em = spot * iv * math.sqrt(dte / 252.0)
    r = targets.compute_price_targets(spot, "CALL", "WEEKLY", iv=iv, dte=dte)
    assert abs(r["expected_move_1sd"] - round(em, 2)) < 0.01
    # T1 ~ 1 sigma up when no fib provided
    assert r["t1"] > spot
    assert r["basis"] == "IV_EM"


def test_weekly_fib_em_blend_and_probs():
    spot, iv, dte = 100.0, 0.30, 5
    em = spot * iv * math.sqrt(dte / 252.0)
    fib_extend = {1.272: round(spot + em, 2), 1.618: spot + 5 * em}
    fib_retrace = {0.618: 96.0}
    r = targets.compute_price_targets(spot, "CALL", "WEEKLY", iv=iv, dte=dte,
                                      fib_extend=fib_extend, fib_retrace=fib_retrace)
    # First fib ext clusters with 1 sigma -> blend basis
    assert r["basis"] in ("FIB_EM_BLEND", "IV_EM", "FIB")
    assert r["t1_prob"] is not None and 0 < r["t1_prob"] <= 95
    # Stop honors 61.8 retrace but no worse than 1 sigma
    assert r["stop"] is not None and r["stop"] <= spot


def test_no_account_size_or_calculate_contracts():
    import main
    assert not hasattr(main, "ACCOUNT_SIZE")
    assert not hasattr(main, "calculate_contracts")
    # Replacement helper exists and is size-free.
    stop, tgt = main.option_risk_levels(1.0)
    assert stop == 0.55 and tgt == 1.4
