# Offline tests for index_options.compute_insights_from_chain: 0DTE
# expected move from the ATM straddle, front-expiry OI structure, gamma
# walls, and the AM/PM duplicate-root handling.

from datetime import date, timedelta

import index_options


# Real today, not a pinned date: the gamma-wall path delegates to
# gamma_exposure.compute_gex_from_chain, which dates contracts off the
# actual wall clock — a pinned TODAY turns every expiry stale (dte < 0)
# once that date passes and the walls silently compute to None.
TODAY = date.today()


def _c(strike, expiry, typ, oi, price, iv=0.18):
    return {
        "strike": float(strike),
        "expiry": expiry,
        "type":   typ,
        "open_interest": oi,
        "price":  price,
        "implied_volatility": iv,
    }


def _chain():
    """SPX-like chain: today's 0DTE expiry plus one more."""
    t = TODAY.isoformat()
    nxt = (TODAY + timedelta(days=1)).isoformat()
    return [
        # 0DTE strikes around spot 6300
        _c(6250, t, "put",  20000, 6.0),
        _c(6300, t, "put",  15000, 18.0),
        _c(6300, t, "call", 10000, 20.0),
        _c(6350, t, "call", 25000, 5.5),
        _c(6400, t, "call",  8000, 1.2),
        _c(6200, t, "put",  12000, 2.5),
        # next expiry
        _c(6300, nxt, "call", 5000, 32.0),
        _c(6300, nxt, "put",  5000, 30.0),
    ]


def test_expected_move_from_atm_straddle():
    ins = index_options.compute_insights_from_chain(
        _chain(), spot=6300.0, index="SPX", today=TODAY)
    assert ins["is_0dte"] is True
    assert ins["dte"] == 0
    assert ins["front_expiry"] == TODAY.isoformat()
    assert ins["atm_strike"] == 6300
    assert ins["expected_move_pts"] == 38.0        # 20 call + 18 put
    assert abs(ins["expected_move_pct"] - 0.60) < 0.02
    assert ins["em_low"] == 6262.0
    assert ins["em_high"] == 6338.0


def test_front_expiry_oi_structure():
    ins = index_options.compute_insights_from_chain(
        _chain(), spot=6300.0, index="SPX", today=TODAY)
    assert ins["front_call_oi"] == 10000 + 25000 + 8000
    assert ins["front_put_oi"] == 20000 + 15000 + 12000
    assert ins["pc_oi"] == round(47000 / 43000, 2)
    # Top strikes ranked by OI
    assert ins["top_call_oi"][0] == [6350, 25000]
    assert ins["top_put_oi"][0] == [6250, 20000]


def test_gamma_walls_computed():
    ins = index_options.compute_insights_from_chain(
        _chain(), spot=6300.0, index="SPX", today=TODAY)
    # gamma_exposure is importable in the test env, so walls must be set:
    # biggest positive-GEX strike above spot / negative below spot.
    assert ins["call_wall"] == 6350
    assert ins["put_wall"] == 6250
    assert ins["gex_regime"] in ("LONG_GAMMA", "SHORT_GAMMA")


def test_duplicate_am_pm_roots_prefer_higher_oi():
    t = TODAY.isoformat()
    chain = [
        # Same strike/date listed by both roots (SPX AM + SPXW PM):
        # the PM contract carries the OI and must win the straddle price.
        _c(6300, t, "call",   100, 99.0),     # stale AM print
        _c(6300, t, "call", 20000, 21.0),     # liquid PM contract
        _c(6300, t, "put",    150, 88.0),
        _c(6300, t, "put",  18000, 17.0),
    ]
    ins = index_options.compute_insights_from_chain(
        chain, spot=6300.0, index="SPX", today=TODAY)
    assert ins["expected_move_pts"] == 38.0        # 21 + 17, not 99 + 88


def test_next_expiry_used_when_no_0dte():
    nxt = (TODAY + timedelta(days=1)).isoformat()
    chain = [
        _c(6300, nxt, "call", 5000, 32.0),
        _c(6300, nxt, "put",  5000, 30.0),
    ]
    ins = index_options.compute_insights_from_chain(
        chain, spot=6300.0, index="SPX", today=TODAY)
    assert ins["is_0dte"] is False
    assert ins["dte"] == 1
    assert ins["expected_move_pts"] == 62.0


def test_unusable_inputs_return_none():
    assert index_options.compute_insights_from_chain([], 6300.0) is None
    assert index_options.compute_insights_from_chain(_chain(), None) is None
    # All expiries in the past -> no front expiry
    old = [_c(6300, "2020-01-17", "call", 100, 1.0)]
    assert index_options.compute_insights_from_chain(
        old, 6300.0, today=TODAY) is None
