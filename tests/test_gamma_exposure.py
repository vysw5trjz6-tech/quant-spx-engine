from datetime import datetime, timedelta

import pytz

import gamma_exposure as gex


def _expiry(days_out):
    # Match prod: gamma_exposure computes DTE from America/New_York's date,
    # so the fixture must anchor to the same tz or 0DTE entries spill into
    # the 1-7 bucket whenever this test runs after ~04:00 UTC.
    et    = pytz.timezone("America/New_York")
    today = datetime.now(et).date()
    return (today + timedelta(days=days_out)).isoformat()


def test_bs_gamma_zero_inputs_returns_zero():
    assert gex.bs_gamma(0,   100, 0.1, 0.2) == 0.0
    assert gex.bs_gamma(100, 0,   0.1, 0.2) == 0.0
    assert gex.bs_gamma(100, 100, 0,   0.2) == 0.0
    assert gex.bs_gamma(100, 100, 0.1, 0)   == 0.0


def test_bs_gamma_atm_peak():
    # Gamma is maximal near the money. Verify ATM > OTM.
    atm = gex.bs_gamma(spot=100, strike=100, dte_years=30/365, iv=0.20)
    otm = gex.bs_gamma(spot=100, strike=120, dte_years=30/365, iv=0.20)
    assert atm > otm > 0


def test_compute_gex_from_chain_signs_match_convention():
    # Convention: calls add to GEX, puts subtract.
    spot  = 500.0
    chain = [
        {"strike": 500, "expiry": _expiry(30), "type": "call",
         "open_interest": 10_000, "implied_volatility": 0.18},
        {"strike": 500, "expiry": _expiry(30), "type": "put",
         "open_interest": 10_000, "implied_volatility": 0.18},
    ]
    out = gex.compute_gex_from_chain(chain, spot)
    # Equal call + put OI at same strike/IV roughly cancels.
    assert abs(out["total_gex"]) < 1e6


def test_compute_gex_by_tenor_buckets():
    spot  = 500.0
    chain = [
        # 0DTE call wall
        {"strike": 505, "expiry": _expiry(0),  "type": "call",
         "open_interest": 50_000, "implied_volatility": 0.30},
        # 1-7 DTE put wall
        {"strike": 495, "expiry": _expiry(5),  "type": "put",
         "open_interest": 30_000, "implied_volatility": 0.20},
        # 30+ DTE small position
        {"strike": 500, "expiry": _expiry(60), "type": "call",
         "open_interest": 5_000,  "implied_volatility": 0.18},
    ]
    out = gex.compute_gex_by_tenor(chain, spot)
    assert set(out.keys()) == {"0DTE", "1-7", "8-30", "30+"}
    # 0DTE bucket should be positive (call OI) and dominate the others
    assert out["0DTE"]["gex"] > 0
    # 1-7 bucket should be negative (put OI)
    assert out["1-7"]["gex"] < 0
    # 8-30 bucket should be empty
    assert out["8-30"]["gex"] == 0
    # 30+ bucket has a small positive contribution
    assert out["30+"]["gex"] > 0


def test_compute_gex_handles_empty_chain():
    assert gex.compute_gex_from_chain([],   500.0) is None
    assert gex.compute_gex_from_chain(None, 500.0) is None
    assert gex.compute_gex_by_tenor([],     500.0) is None
