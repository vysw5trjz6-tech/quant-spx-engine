"""Horizon-aware underlying price-target / expected-move framework.

One entry point, compute_price_targets(), produces underlying T1/T2/stop levels
plus approximate touch probabilities and a `basis` label describing how the
targets were derived. Two horizons:

  INTRADAY  -- preserves the scanner's existing ORB-multiple / ATR math so
               0DTE behavior is unchanged.
  WEEKLY    -- IV-implied expected move (em = spot * iv * sqrt(dte/252))
               blended with Fibonacci extensions; stop anchored on the 61.8%
               retrace, clamped to no worse than 1 sigma.

Pure math -- no project imports beyond vol_math's normal CDF, so it is safe to
import anywhere (no circular dependency on main).
"""
import math

try:
    from vol_math import _norm_cdf
except Exception:  # pragma: no cover - fallback if vol_math unavailable
    def _norm_cdf(x):
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _sign(direction):
    return 1.0 if str(direction).upper() == "CALL" else -1.0


def _touch_prob(distance, expected_move_1sd):
    """Approximate probability the underlying *touches* a level `distance`
    away within the horizon, given a 1-sigma expected move.

    For a driftless diffusion the first-passage (touch) probability of a level
    d away is ~2*(1 - Phi(d/sigma)) (reflection principle). We clamp to a
    sane [2, 95] band. This is an approximation, not a tradable Greek.
    """
    if not expected_move_1sd or expected_move_1sd <= 0 or distance is None:
        return None
    d_sigma = abs(distance) / expected_move_1sd
    p = 2.0 * (1.0 - _norm_cdf(d_sigma))
    return int(round(max(2.0, min(95.0, p * 100.0))))


def _fib_levels_in_direction(spot, sign, fib_extend):
    """Extension prices beyond spot in the trade direction, sorted nearest-first."""
    if not fib_extend:
        return []
    vals = []
    for v in fib_extend.values():
        if v is None:
            continue
        if (sign > 0 and v > spot) or (sign < 0 and v < spot):
            vals.append(float(v))
    vals.sort(key=lambda x: abs(x - spot))
    return vals


def _retrace_618(fib_retrace):
    """Pull the 61.8% retrace level under whatever key convention is used."""
    if not fib_retrace:
        return None
    for k in (0.618, "0.618", "61.8", "618"):
        if k in fib_retrace and fib_retrace[k] is not None:
            return float(fib_retrace[k])
    return None


def _intraday_targets(spot, sign, orb_range, atr, avg_range):
    """Reproduces the legacy ORB-multiple / ATR target math (0DTE unchanged)."""
    basis = None
    if orb_range and orb_range > 0:
        t1   = spot + sign * orb_range
        t2   = spot + sign * orb_range * 2.0
        stop = spot - sign * orb_range * 0.5
        basis = "ORB"
        t1_prob = t2_prob = None
        if avg_range and avg_range > 0:
            t1_prob = int(round(max(20, min(85, 100 - (orb_range / avg_range * 100)))))
            t2_prob = int(round(max(10, min(55, 100 - (orb_range * 2 / avg_range * 100)))))
    elif atr and atr > 0:
        t1   = spot + sign * atr * 1.5
        t2   = spot + sign * atr * 3.0
        stop = spot - sign * atr * 0.75
        basis = "ATR"
        t1_prob, t2_prob = 50, 25
    else:
        return None
    return {
        "t1": round(t1, 2), "t2": round(t2, 2), "stop": round(stop, 2),
        "t1_prob": t1_prob, "t2_prob": t2_prob,
        "basis": basis, "expected_move_1sd": None, "horizon": "INTRADAY",
    }


def _weekly_targets(spot, sign, iv, dte, fib_extend, fib_retrace):
    em = None
    if iv and iv > 0 and dte and dte > 0:
        em = spot * iv * math.sqrt(dte / 252.0)

    fib_vals = _fib_levels_in_direction(spot, sign, fib_extend)
    fib1 = fib_vals[0] if len(fib_vals) >= 1 else None
    fib2 = fib_vals[1] if len(fib_vals) >= 2 else None

    em_t1 = spot + sign * 1.000 * em if em else None
    em_t2 = spot + sign * 1.618 * em if em else None

    def _nearer(em_lvl, fib_lvl):
        # Choose the more conservative (nearer-to-spot) candidate; return
        # (chosen_price, which) where which in {"em","fib","blend"}.
        if em_lvl is not None and fib_lvl is not None:
            if abs(em_lvl - fib_lvl) / spot < 0.005:
                return (em_lvl + fib_lvl) / 2.0, "blend"
            return (em_lvl, "em") if abs(em_lvl - spot) <= abs(fib_lvl - spot) else (fib_lvl, "fib")
        if em_lvl is not None:
            return em_lvl, "em"
        if fib_lvl is not None:
            return fib_lvl, "fib"
        return None, None

    t1, w1 = _nearer(em_t1, fib1)
    t2, w2 = _nearer(em_t2, fib2)

    if "blend" in (w1, w2):
        basis = "FIB_EM_BLEND"
    elif w1 == "fib" or w2 == "fib":
        basis = "FIB" if not em else "FIB_EM_BLEND"
    elif w1 == "em" or w2 == "em":
        basis = "IV_EM"
    else:
        basis = "NONE"

    # Stop on the 61.8% retrace, clamped to no worse than 1 sigma from spot.
    stop = _retrace_618(fib_retrace)
    one_sd_stop = (spot - sign * em) if em else None
    if stop is None:
        stop = one_sd_stop
    elif one_sd_stop is not None:
        # "no worse than 1 sigma": don't let the stop sit further than 1sd away.
        if sign > 0:
            stop = max(stop, one_sd_stop)
        else:
            stop = min(stop, one_sd_stop)

    if t1 is None and t2 is None:
        return None

    return {
        "t1": round(t1, 2) if t1 is not None else None,
        "t2": round(t2, 2) if t2 is not None else None,
        "stop": round(stop, 2) if stop is not None else None,
        "t1_prob": _touch_prob(t1 - spot if t1 is not None else None, em),
        "t2_prob": _touch_prob(t2 - spot if t2 is not None else None, em),
        "basis": basis,
        "expected_move_1sd": round(em, 2) if em else None,
        "horizon": "WEEKLY",
    }


def compute_price_targets(spot, direction, horizon, atr=None, orb_range=None,
                          vwap_bands=None, iv=None, dte=None,
                          fib_extend=None, fib_retrace=None, avg_range=None):
    """Return underlying targets for a signal.

    Returns dict: {t1, t2, stop, t1_prob, t2_prob, basis, expected_move_1sd,
    horizon} (sign-aware on direction), or None when there is not enough
    information to size a target.
    """
    if not spot or spot <= 0 or direction not in ("CALL", "PUT"):
        return None
    sign = _sign(direction)

    if horizon == "WEEKLY":
        return _weekly_targets(spot, sign, iv, dte, fib_extend, fib_retrace)
    return _intraday_targets(spot, sign, orb_range, atr, avg_range)
