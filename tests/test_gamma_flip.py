"""Gamma flip via spot ladder (gamma_exposure.find_gamma_flip).

The flip level is DEFINED as the spot at which net dealer gamma crosses
zero. So the load-bearing assertion in this file is simply:

    net_gex_at_spot(curve, find_gamma_flip(curve, spot)) ~= 0

Everything else is bracketing, degenerate-book handling, and a regression
guard recording how far the previous cumulative-by-strike method sat from
that root on a multi-expiry book.
"""
import math

import pytest

from gamma_exposure import (
    bs_gamma,
    compute_gex_from_chain,
    cumulative_flip_strike,
    find_gamma_flip,
    net_gex_at_spot,
)


SPOT = 6300.0


# ---------------------------------------------------------------------------
# Book builders
# ---------------------------------------------------------------------------

def _book(expiries, spot=SPOT, lo=5400, hi=7200, step=25):
    """(curve, by_strike) for an SPX-shaped book.

    Heavy OTM put wing (crash hedges) + lighter call wing + downside vol
    skew, replicated across each (dte, weight) in `expiries`. Matches the
    shape both production call sites actually see: gamma_exposure pulls
    expiries_ahead=3, gex_intraday runs max_dte=5.
    """
    curve, by_strike = [], {}
    for dte, wt in expiries:
        t_years = max(dte, 0.5) / 365.0
        for k in range(lo, hi + 1, step):
            m = k / spot
            put_oi = int(wt * (9000 * math.exp(-((m - 0.93) ** 2) / 0.0009) + 600))
            call_oi = int(wt * (3500 * math.exp(-((m - 1.02) ** 2) / 0.0008) + 250))
            iv = 0.14 + 0.55 * max(0.0, 1.0 - m)
            for oi, sign in ((put_oi, -1), (call_oi, 1)):
                curve.append((float(k), t_years, iv, oi * sign))
                g = (bs_gamma(spot, k, t_years, iv)
                     * oi * sign * 100 * spot * spot * 0.01)
                by_strike[float(k)] = by_strike.get(float(k), 0.0) + g
    return curve, by_strike


def _scale(curve, spot=SPOT):
    """Magnitude of the book, for relative-tolerance comparisons."""
    return max(abs(net_gex_at_spot(curve, spot * 0.97)),
               abs(net_gex_at_spot(curve, spot * 1.03)))


SINGLE_EXPIRY = [(0, 1.0)]
WEEKLIES      = [(0, 1.0), (7, 0.8), (14, 0.6)]
TERM_STRUCTURE = [(0, 1.0), (7, 0.8), (30, 1.4), (60, 1.1), (90, 0.9)]


# ---------------------------------------------------------------------------
# The defining property
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("expiries", [SINGLE_EXPIRY, WEEKLIES, TERM_STRUCTURE])
def test_flip_is_a_root_of_net_gex(expiries):
    curve, _ = _book(expiries)
    flip = find_gamma_flip(curve, SPOT)
    assert flip is not None
    # Stated as a price bracket, not a residual tolerance: on a 0DTE book
    # gamma is steep enough that a sub-cent error in spot still leaves a
    # six-figure residual, which says nothing about solver accuracy.
    assert net_gex_at_spot(curve, flip - 0.01) * \
        net_gex_at_spot(curve, flip + 0.01) < 0


@pytest.mark.parametrize("expiries", [SINGLE_EXPIRY, WEEKLIES, TERM_STRUCTURE])
def test_flip_brackets_a_genuine_sign_change(expiries):
    curve, _ = _book(expiries)
    flip = find_gamma_flip(curve, SPOT)
    below = net_gex_at_spot(curve, flip * 0.995)
    above = net_gex_at_spot(curve, flip * 1.005)
    assert below * above < 0, "flip must separate short- from long-gamma"


def test_flip_does_not_depend_on_the_spot_it_was_evaluated_from():
    # The flip is a property of POSITIONING, not of where spot happens to
    # be. The fast loop relies on this: it reuses a profile's flip while
    # live spot moves, without rebuilding the curve.
    curve, _ = _book(WEEKLIES)
    ref = find_gamma_flip(curve, SPOT)
    for probe in (SPOT * 0.98, SPOT * 0.99, SPOT * 1.01, SPOT * 1.02):
        assert abs(find_gamma_flip(curve, probe) - ref) < 0.5


# ---------------------------------------------------------------------------
# Why this replaced the cumulative-by-strike method
# ---------------------------------------------------------------------------

def test_cumulative_method_is_not_a_root_on_multi_expiry_books():
    """Regression guard for the bug this change fixes.

    On a single-expiry book the old cumulative-by-strike crossing lands
    almost exactly on the true root -- gamma is peaked hard enough at each
    strike that the running-sum crossing coincides with it. That is why
    the wrong method looked plausible for so long.

    Add a term structure and it comes apart: the crossing drifts far from
    the root and reports a level where dealers are nowhere near flat.
    """
    single, single_by_strike = _book(SINGLE_EXPIRY)
    assert abs(find_gamma_flip(single, SPOT)
               - cumulative_flip_strike(single_by_strike)) < 2.0

    multi, multi_by_strike = _book(TERM_STRUCTURE)
    ladder = find_gamma_flip(multi, SPOT)
    legacy = cumulative_flip_strike(multi_by_strike)

    # Tens of points apart on SPX -- more than the flip_band_pct the
    # regime state machine uses to decide it is safely on one side.
    assert abs(ladder - legacy) > 50.0
    # And the legacy point is not a root at all: net gamma there is a
    # sizeable fraction of the whole book.
    assert abs(net_gex_at_spot(multi, legacy)) > 0.1 * _scale(multi)


# ---------------------------------------------------------------------------
# Degenerate books
# ---------------------------------------------------------------------------

def test_one_way_book_has_no_flip():
    # Calls only: dealer gamma is positive everywhere, never crosses.
    curve = [(float(k), 5 / 365.0, 0.18, 1000)
             for k in range(6000, 6600, 25)]
    assert find_gamma_flip(curve, SPOT) is None
    # Puts only: negative everywhere.
    curve = [(float(k), 5 / 365.0, 0.18, -1000)
             for k in range(6000, 6600, 25)]
    assert find_gamma_flip(curve, SPOT) is None


def test_empty_and_invalid_inputs():
    curve, _ = _book(WEEKLIES)
    assert find_gamma_flip([], SPOT) is None
    assert find_gamma_flip(None, SPOT) is None
    assert find_gamma_flip(curve, None) is None
    assert find_gamma_flip(curve, 0) is None
    assert find_gamma_flip(curve, -100) is None


def test_flip_outside_the_search_band_is_reported_as_none():
    # A book whose entire crossing sits far above the +/-15% window.
    curve, _ = _book(WEEKLIES, spot=SPOT)
    assert find_gamma_flip(curve, SPOT * 3.0) is None


def test_nearest_crossing_wins_when_a_book_flips_more_than_once():
    # Put cluster low, call cluster mid, put cluster high -> net gamma
    # changes sign more than once across the ladder.
    t = 5 / 365.0
    curve = (
        [(5700.0, t, 0.30, -60000)]
        + [(float(k), t, 0.20, 9000) for k in range(6250, 6360, 25)]
        + [(6900.0, t, 0.16, -60000)]
    )
    lo, hi = SPOT * 0.85, SPOT * 1.15
    xs = [lo + i * (hi - lo) / 600 for i in range(601)]
    vals = [net_gex_at_spot(curve, s) for s in xs]
    roots = [(xs[i] + xs[i + 1]) / 2 for i in range(600)
             if vals[i] * vals[i + 1] < 0]
    assert len(roots) >= 2, "fixture must actually cross more than once"

    flip = find_gamma_flip(curve, SPOT)
    assert flip is not None
    nearest = min(roots, key=lambda r: abs(r - SPOT))
    assert abs(flip - nearest) < 5.0


# ---------------------------------------------------------------------------
# net_gex_at_spot
# ---------------------------------------------------------------------------

def test_net_gex_sign_convention():
    t = 1 / 365.0
    calls = [(6300.0, t, 0.18, 1000)]
    puts = [(6300.0, t, 0.18, -1000)]
    assert net_gex_at_spot(calls, SPOT) > 0
    assert net_gex_at_spot(puts, SPOT) < 0
    assert net_gex_at_spot(calls + puts, SPOT) == pytest.approx(0.0, abs=1e-6)


def test_net_gex_degenerate_inputs():
    curve, _ = _book(SINGLE_EXPIRY)
    assert net_gex_at_spot([], SPOT) == 0.0
    assert net_gex_at_spot(None, SPOT) == 0.0
    assert net_gex_at_spot(curve, 0) == 0.0
    assert net_gex_at_spot(curve, None) == 0.0


def test_net_gex_tolerates_extra_trailing_row_fields():
    # gex_intraday rows carry a 5th element (dte); gamma_exposure's carry
    # four. Both must work through the same function.
    t = 1 / 365.0
    four = [(6300.0, t, 0.18, 1000)]
    five = [(6300.0, t, 0.18, 1000, 0)]
    assert net_gex_at_spot(four, SPOT) == net_gex_at_spot(five, SPOT)


def test_net_gex_far_from_the_book_decays_to_zero():
    curve, _ = _book(SINGLE_EXPIRY)
    assert abs(net_gex_at_spot(curve, SPOT * 3.0)) < 1e-3 * _scale(curve)


# ---------------------------------------------------------------------------
# End-to-end through compute_gex_from_chain
# ---------------------------------------------------------------------------

def _chain(expiries):
    """Chain rows in the shape _prep_contract expects."""
    from datetime import datetime, timedelta

    import pytz

    today = datetime.now(pytz.timezone("America/New_York")).date()
    rows = []
    for dte, wt in expiries:
        expiry = (today + timedelta(days=dte)).isoformat()
        for k in range(5800, 6801, 25):
            m = k / SPOT
            put_oi = int(wt * (9000 * math.exp(-((m - 0.93) ** 2) / 0.0009) + 600))
            call_oi = int(wt * (3500 * math.exp(-((m - 1.02) ** 2) / 0.0008) + 250))
            iv = 0.14 + 0.55 * max(0.0, 1.0 - m)
            rows.append({"strike": k, "expiry": expiry, "type": "put",
                         "open_interest": put_oi, "implied_volatility": iv})
            rows.append({"strike": k, "expiry": expiry, "type": "call",
                         "open_interest": call_oi, "implied_volatility": iv})
    return rows


def test_compute_gex_reports_both_flip_measures():
    got = compute_gex_from_chain(_chain(WEEKLIES), SPOT)
    assert got is not None
    assert got["zero_gamma_strike"] is not None
    assert got["zero_gamma_cumulative"] is not None
    # The headline value is the ladder solve, and it differs from the
    # legacy one it replaced.
    assert got["zero_gamma_strike"] != got["zero_gamma_cumulative"]


def test_compute_gex_flip_is_a_root_end_to_end():
    got = compute_gex_from_chain(_chain(WEEKLIES), SPOT)
    # Rebuild the same curve the function built internally and confirm the
    # reported level really zeroes it. compute_gex_from_chain rounds to 2dp
    # for display, so allow a bracket a little wider than that rounding.
    curve, _ = _book(WEEKLIES, lo=5800, hi=6800)
    flip = got["zero_gamma_strike"]
    assert net_gex_at_spot(curve, flip - 0.02) * \
        net_gex_at_spot(curve, flip + 0.02) < 0


def test_compute_gex_survives_a_book_with_no_flip():
    chain = [{"strike": k, "expiry": None, "type": "call",
              "open_interest": 1000, "implied_volatility": 0.18}
             for k in range(6000, 6600, 25)]
    from datetime import datetime, timedelta

    import pytz
    today = datetime.now(pytz.timezone("America/New_York")).date()
    for row in chain:
        row["expiry"] = (today + timedelta(days=2)).isoformat()

    got = compute_gex_from_chain(chain, SPOT)
    assert got is not None
    assert got["zero_gamma_strike"] is None      # honest, not fabricated
    assert got["regime"] == "LONG_GAMMA"
    assert got["total_gex"] > 0
