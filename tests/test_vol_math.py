"""Black-Scholes price + implied-vol solver round-trip tests."""
import math
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import vol_math


# ---- Black-Scholes price sanity ----------------------------------------

def test_bs_call_atm_no_drift():
    # ATM, no rate, no dividend: price ~= 0.4 * S * sigma * sqrt(T)
    S, K, T, r, sigma = 100.0, 100.0, 0.25, 0.0, 0.20
    p = vol_math.bs_price(S, K, T, r, sigma, "call")
    expected_approx = 0.4 * S * sigma * math.sqrt(T)
    assert abs(p - expected_approx) < expected_approx * 0.1


def test_put_call_parity():
    # C - P = S - K * exp(-rT) at any sigma
    S, K, T, r, sigma = 100.0, 105.0, 0.5, 0.05, 0.25
    c = vol_math.bs_price(S, K, T, r, sigma, "call")
    p = vol_math.bs_price(S, K, T, r, sigma, "put")
    parity = S - K * math.exp(-r * T)
    assert abs((c - p) - parity) < 1e-8


def test_intrinsic_at_expiry():
    assert vol_math.bs_price(100, 90, 0, 0.05, 0.30, "call") == 10.0
    assert vol_math.bs_price(100, 110, 0, 0.05, 0.30, "put") == 10.0
    assert vol_math.bs_price(100, 110, 0, 0.05, 0.30, "call") == 0.0


# ---- IV solver round-trip ----------------------------------------------

@pytest.mark.parametrize("sigma", [0.10, 0.18, 0.30, 0.50, 0.80])
@pytest.mark.parametrize("dte_days", [7, 21, 60, 180])
@pytest.mark.parametrize("moneyness", [0.95, 1.00, 1.05])
@pytest.mark.parametrize("typ", ["call", "put"])
def test_iv_round_trip(sigma, dte_days, moneyness, typ):
    S, r = 100.0, 0.05
    K = S * moneyness
    T = dte_days / 365.0
    price = vol_math.bs_price(S, K, T, r, sigma, typ)
    iv = vol_math.implied_vol(price, S, K, T, r, typ)
    assert iv is not None
    # 50 bp tolerance covers Newton degeneracies on deep-ITM short tenors
    assert abs(iv - sigma) < 0.005, (
        "round-trip drift: sigma={} dte={} mny={} typ={} -> {}"
        .format(sigma, dte_days, moneyness, typ, iv)
    )


def test_iv_returns_none_on_arbitrage_violation():
    # Call below intrinsic floor
    S, K, T, r = 100.0, 90.0, 0.25, 0.05
    floor = max(S - K * math.exp(-r * T), 0.0)
    assert vol_math.implied_vol(floor - 0.5, S, K, T, r, "call") is None
    # Negative price
    assert vol_math.implied_vol(-1.0, S, K, T, r, "call") is None
    # Zero time
    assert vol_math.implied_vol(5.0, S, K, 0.0, r, "call") is None
