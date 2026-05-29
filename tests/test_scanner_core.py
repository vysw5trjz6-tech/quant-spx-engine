"""Tests for the unified scanner orchestration helpers."""
import scanner_core


def _sig(symbol, horizon, prob, conviction=1.0, product_class="ETF", rs=0.0):
    return {"symbol": symbol, "horizon": horizon, "prob": prob,
            "conviction": conviction, "product_class": product_class,
            "rs": rs, "direction": "CALL", "signal_type": "ORB"}


def test_conviction_adjusted_score():
    s = _sig("SPY", "WEEKLY", 60, conviction=1.2)
    assert abs(scanner_core.conviction_adjusted_score(s) - 72.0) < 1e-9


def test_merge_dedupes_by_symbol_horizon_and_ranks():
    weekly = [
        _sig("AAPL", "WEEKLY", 55, conviction=1.0, product_class="STOCK", rs=2.0),
        _sig("SPY",  "WEEKLY", 50, conviction=1.2),
        _sig("SPY",  "WEEKLY", 99),  # duplicate (symbol, horizon) -> dropped
    ]
    intraday = [_sig("SPY", "INTRADAY", 70, conviction=1.1)]
    out = scanner_core.merge_and_rank(intraday, weekly, {"k": "v"})

    # Dedup: only one weekly SPY survives.
    spy_weekly = [s for s in out["weekly"] if s["symbol"] == "SPY"]
    assert len(spy_weekly) == 1
    # SPY (50*1.2=60) ranks above AAPL (55*1.0=55).
    assert out["weekly"][0]["symbol"] == "SPY"
    # Intraday tier preserved separately + context passed through.
    assert out["intraday"][0]["symbol"] == "SPY"
    assert out["context"] == {"k": "v"}


def test_rank_secondary_universe_stocks_by_rs():
    weekly = [
        _sig("NVDA", "WEEKLY", 60, product_class="STOCK", rs=1.0),
        _sig("AMD",  "WEEKLY", 60, product_class="STOCK", rs=3.0),
        _sig("SPY",  "WEEKLY", 60, product_class="ETF",   rs=5.0),
    ]
    ranked = scanner_core.rank_secondary_universe(weekly)
    assert [s["symbol"] for s in ranked] == ["AMD", "NVDA"]  # ETF excluded


def test_build_rationale_minimal_and_summary():
    s = _sig("SPY", "WEEKLY", 62, conviction=1.1)
    s.update({"t1": 510.0, "t1_prob": 40, "week_expiry": "2026-06-05",
              "dte": 4, "expected_move": 6.5, "atm_iv": 0.18})
    r = scanner_core.build_rationale(s)
    assert r["signal_type"] == "ORB"
    assert r["targets"]["t1"] == 510.0
    assert "SPY" in r["summary"]
    assert r["expected_move"]["one_sd"] == 6.5
