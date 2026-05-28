"""Regression tests for:

  #4 GEX correctness + Databento resilience
     - compute_gex_from_chain / by_tenor solve IV from a per-contract price
       when the feed supplies no implied vol (the statistics chain only has
       OI). Before this, every contract was dropped (iv <= 0) and GEX
       silently produced nothing even when the chain loaded.
     - Databento price scaling, transient-vs-billing error classification,
       and the retry wrapper (transient retries, billing raises immediately).

  #5 Late-day 0DTE suppression
     - get_liquid_option returns "no contract" past the 0DTE cutoff once a
       same-day chain is confirmed, and the scan keeps the row WATCHING.
"""
import ast
import os

import pytest

from datetime import datetime, timedelta
import pytz

import gamma_exposure as gex
import databento_adapter as da


def _exp(days_out):
    et = pytz.timezone("America/New_York")
    return (datetime.now(et).date() + timedelta(days=days_out)).isoformat()


# ---------------------------------------------------------------------------
# #4 -- GEX solves IV from price (behavioral)
# ---------------------------------------------------------------------------

def test_gex_computes_from_price_without_iv():
    spot = 500.0
    chain = [
        {"strike": 505, "expiry": _exp(7), "type": "call",
         "open_interest": 40_000, "price": 3.20, "implied_volatility": None},
        {"strike": 495, "expiry": _exp(7), "type": "put",
         "open_interest": 45_000, "price": 3.40, "implied_volatility": None},
        {"strike": 500, "expiry": _exp(30), "type": "call",
         "open_interest": 20_000, "price": 9.10, "implied_volatility": None},
    ]
    out = gex.compute_gex_from_chain(chain, spot)
    assert out is not None, "GEX must compute when price is present but IV is not"
    assert out["total_gex"] != 0
    assert out["call_wall"] == 505.0
    assert out["put_wall"] == 495.0


def test_gex_none_without_iv_and_without_price():
    # No IV and no price -> genuinely cannot compute -> None (not a crash).
    spot = 500.0
    chain = [{"strike": 505, "expiry": _exp(7), "type": "call",
              "open_interest": 40_000, "implied_volatility": None}]
    assert gex.compute_gex_from_chain(chain, spot) is None


def test_gex_prefers_supplied_iv_over_price_solve():
    # When IV is supplied it should be used directly (price solve not needed).
    spot = 500.0
    chain = [{"strike": 500, "expiry": _exp(30), "type": "call",
              "open_interest": 10_000, "implied_volatility": 0.18}]
    out = gex.compute_gex_from_chain(chain, spot)
    assert out is not None and out["total_gex"] != 0


def test_gex_by_tenor_solves_from_price():
    spot = 500.0
    chain = [
        {"strike": 505, "expiry": _exp(0), "type": "call",
         "open_interest": 50_000, "price": 1.10, "implied_volatility": None},
        {"strike": 495, "expiry": _exp(5), "type": "put",
         "open_interest": 30_000, "price": 2.40, "implied_volatility": None},
    ]
    out = gex.compute_gex_by_tenor(chain, spot)
    assert set(out.keys()) == {"0DTE", "1-7", "8-30", "30+"}
    assert out["0DTE"]["gex"] != 0


def test_prep_contract_rejects_absurd_iv():
    # A price that solves to an absurd IV (>5.0) is dropped.
    spot = 500.0
    today = datetime.now(pytz.timezone("America/New_York")).date()
    # Deep OTM near-dated with a huge price -> unsolvable / absurd.
    bad = {"strike": 400, "expiry": _exp(1), "type": "call",
           "open_interest": 1000, "price": 0.01, "implied_volatility": None}
    res = gex._prep_contract(bad, spot, today)
    # Either unsolved (None) or clamped out; must not yield a usable iv.
    assert res is None or res[3] <= 5.0


# ---------------------------------------------------------------------------
# #4 -- Databento price scaling + error classification (behavioral)
# ---------------------------------------------------------------------------

def test_scale_dbn_price_float_and_fixed_point():
    assert da._scale_dbn_price(4.91) == 4.91
    assert da._scale_dbn_price(4910000000) == pytest.approx(4.91)


def test_scale_dbn_price_rejects_undef_and_nonpositive():
    assert da._scale_dbn_price(9223372036854775807) is None  # i64 max UNDEF
    assert da._scale_dbn_price(0) is None
    assert da._scale_dbn_price(-1) is None
    assert da._scale_dbn_price(None) is None


def test_transient_vs_billing_classification():
    assert da._is_transient_error(Exception("504 The remote gateway timed out."))
    assert da._is_transient_error(Exception("503 Service Unavailable"))
    assert da._is_billing_error(Exception("402 account_insufficient_funds"))
    # A billing error must never be treated as transient (would retry forever).
    assert not da._is_transient_error(Exception("402 account_insufficient_funds"))


def test_pull_with_retry_retries_transient_then_succeeds(monkeypatch):
    monkeypatch.setattr(da.time, "sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Exception("504 gateway timed out")
        return "ok"

    assert da._pull_with_retry(flaky, retries=2, backoff=0.0) == "ok"
    assert calls["n"] == 3


def test_pull_with_retry_raises_billing_immediately(monkeypatch):
    monkeypatch.setattr(da.time, "sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def billing():
        calls["n"] += 1
        raise Exception("402 account_insufficient_funds")

    with pytest.raises(Exception):
        da._pull_with_retry(billing, retries=3, backoff=0.0)
    assert calls["n"] == 1, "billing error must not be retried"


def test_chain_snapshot_accepts_with_price_kwarg():
    # Signature contract: GEX calls this with with_price=True.
    import inspect
    sig = inspect.signature(da.get_options_chain_snapshot)
    assert "with_price" in sig.parameters


# ---------------------------------------------------------------------------
# #5 -- late-day 0DTE suppression (structural; main.py boots threads on import)
# ---------------------------------------------------------------------------

MAIN_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "main.py")


@pytest.fixture(scope="module")
def main_src():
    with open(MAIN_PATH, "r", encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def main_tree(main_src):
    return ast.parse(main_src)


def _find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def test_get_liquid_option_takes_cutoff_params(main_tree):
    fn = _find_func(main_tree, "get_liquid_option")
    args = [a.arg for a in fn.args.args]
    assert "et_hour" in args and "zero_dte_cutoff" in args


def test_get_liquid_option_suppresses_0dte_past_cutoff(main_src, main_tree):
    fn = _find_func(main_tree, "get_liquid_option")
    body = ast.get_source_segment(main_src, fn)
    # Returns "no contract" for a confirmed same-day chain past cutoff,
    # rather than logging a penny "Selected" pick.
    assert "dte == 0" in body and "et_hour >= zero_dte_cutoff" in body
    assert 'return None, None, False, "0DTE"' in body


def test_scan_passes_cutoff_to_get_liquid_option(main_src):
    assert main_src.count("zero_dte_cutoff=cfg.get(\"zero_dte_cutoff_hour\"") >= 2, \
        "both scan call sites must pass the cutoff through"


def test_watching_demotion_covers_no_options(main_src):
    # The suppressed pick yields "SIGNAL (no options)"; demotion must catch it.
    assert 'result["status"] in ("SIGNAL", "SIGNAL (no options)")' in main_src
