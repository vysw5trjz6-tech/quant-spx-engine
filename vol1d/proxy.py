# vol1d/proxy.py
# VIX1D proxy: Cboe generalized variance-swap formula on the two nearest
# SPXW strips (docs/vix1d_module_spec.md §1).
#
# PROXY ≠ OFFICIAL. This will not match the official Cboe print
# tick-for-tick (delayed quotes, flat rate, simplified roll). We care about
# regime and relative change; vol1d.qa reconciles against the official
# close daily and logs the residual.
#
# All time-to-expiry math is BUSINESS time via vol1d.daycount — never
# calendar time. That convention is the single biggest silent mis-scaling
# risk in a 1-day index (a Friday 0DTE/Monday pair is ~1 business day, not
# 3 calendar days).

import math

from vol1d import config as vol1d_config
from vol1d import daycount


def _mid(bid, ask):
    """Quote midpoint. A one-sided quote (no ask) falls back to the bid so
    a deep-OTM strike with only a resting bid still contributes."""
    if bid <= 0:
        return None
    if ask and ask > 0:
        return (bid + ask) / 2.0
    return bid


def build_strike_table(quotes):
    """{strike: {"call": (bid, ask), "put": (bid, ask)}} for one expiry.
    On duplicate (strike, type) rows keep the better-bid quote."""
    table = {}
    for q in quotes:
        side = q["type"]
        cur = table.setdefault(q["strike"], {}).get(side)
        if cur is None or q["bid"] > cur[0]:
            table[q["strike"]][side] = (q["bid"], q["ask"])
    return table


def forward_and_k0(table, r, t):
    """Forward level and K0 for one term.

    F = K_atm + e^(rT) * (C_mid - P_mid) at the strike where |C_mid - P_mid|
    is smallest (both legs must be bid). K0 = first (largest) strike at or
    below F. Returns (F, K0) or (None, None) when no strike has both legs.
    """
    best_k = best_diff = None
    for k in sorted(table):
        legs = table[k]
        c = _mid(*legs["call"]) if "call" in legs else None
        p = _mid(*legs["put"])  if "put"  in legs else None
        if c is None or p is None:
            continue
        diff = abs(c - p)
        if best_diff is None or diff < best_diff:
            best_diff, best_k = diff, k
    if best_k is None:
        return None, None

    legs = table[best_k]
    f = best_k + math.exp(r * t) * (_mid(*legs["call"]) - _mid(*legs["put"]))
    below = [k for k in table if k < f]
    k0 = max(below) if below else min(table)
    return f, k0


def select_otm_quotes(table, k0, consecutive_no_bid_stop=2):
    """OTM strike selection per the Cboe rule (spec §1).

    Walk outward from K0 — puts below, calls above. Skip zero-bid strikes;
    stop a side after `consecutive_no_bid_stop` CONSECUTIVE no-bid strikes.
    At K0 itself Q is the average of the call and put mids (or whichever
    side is quoted).

    Returns sorted list of (strike, Q) including K0.
    """
    strikes = sorted(table)
    if k0 not in table:
        return []

    def _walk(side, ordered):
        out = []
        misses = 0
        for k in ordered:
            legs = table[k]
            q = _mid(*legs[side]) if side in legs else None
            if q is None:
                misses += 1
                if misses >= consecutive_no_bid_stop:
                    break
                continue
            misses = 0
            out.append((k, q))
        return out

    puts  = _walk("put",  [k for k in reversed(strikes) if k < k0])
    calls = _walk("call", [k for k in strikes if k > k0])

    legs = table[k0]
    k0_mids = [m for m in (_mid(*legs[s]) if s in legs else None
                           for s in ("call", "put")) if m is not None]
    selected = list(puts) + ([(k0, sum(k0_mids) / len(k0_mids))] if k0_mids else []) + calls
    selected.sort(key=lambda kq: kq[0])
    return selected


def delta_k(strikes, i):
    """ΔK_i = half the distance between the neighboring strikes; at either
    end of the strip, the full distance to the single neighbor."""
    if len(strikes) < 2:
        return 0.0
    if i == 0:
        return strikes[1] - strikes[0]
    if i == len(strikes) - 1:
        return strikes[-1] - strikes[-2]
    return (strikes[i + 1] - strikes[i - 1]) / 2.0


def term_variance(table, r, t, consecutive_no_bid_stop=2, min_strikes=3):
    """sigma^2 for one term:

        sigma^2 = (2/T) * sum_i[ dK_i/K_i^2 * e^(RT) * Q(K_i) ]
                - (1/T) * (F/K0 - 1)^2

    Returns dict {var, forward, k0, n_strikes} or None when the strip is
    unusable (no forward, too few bid strikes, or t <= 0).
    """
    if t <= 0:
        return None
    f, k0 = forward_and_k0(table, r, t)
    if f is None:
        return None
    selected = select_otm_quotes(table, k0, consecutive_no_bid_stop)
    if len(selected) < min_strikes:
        return None

    strikes = [k for k, _ in selected]
    ert = math.exp(r * t)
    total = 0.0
    for i, (k, q) in enumerate(selected):
        total += delta_k(strikes, i) / (k * k) * ert * q
    var = (2.0 / t) * total - (1.0 / t) * (f / k0 - 1.0) ** 2
    return {"var": var, "forward": f, "k0": k0, "n_strikes": len(selected)}


def select_term_expiries(quotes, now_et, cfg_proxy, holidays=frozenset()):
    """(near_expiry, next_expiry) — the two nearest expiries with usable
    business time left. The 0DTE strip is dropped once it has fewer than
    min_t1_minutes of business time to settlement (quotes go degenerate),
    rolling near-term onto the next strip."""
    min_t1_years = (cfg_proxy["min_t1_minutes"]
                    / (cfg_proxy["business_day_year"] * daycount.MINUTES_PER_DAY))
    live = []
    for exp in sorted({q["expiry"] for q in quotes}):
        t = daycount.business_time_to_expiry(
            now_et, exp,
            business_day_year=cfg_proxy["business_day_year"],
            settle_hour_et=cfg_proxy["settle_hour_et"],
            holidays=holidays)
        if t >= min_t1_years:
            live.append(exp)
        if len(live) == 2:
            break
    if len(live) < 2:
        return (live[0], None) if live else (None, None)
    return live[0], live[1]


def compute_vix1d(snapshot, now_et=None, cfg=None, holidays=frozenset()):
    """VIX1D proxy from a chain snapshot (vol1d.chain_source format).

    Returns a diagnostics dict:
      {vix1d, spot, ts, t1, t2, w1, w2, near_expiry, next_expiry,
       sigma1_sq, sigma2_sq, f1, f2, k0_1, k0_2, n_strikes_1, n_strikes_2}
    or None (with a printed reason) when the chain can't support the calc.
    """
    if not snapshot or not snapshot.get("quotes"):
        return None
    cfg = cfg or vol1d_config.get_config()
    p = cfg["proxy"]
    now_et = now_et or snapshot.get("ts")
    if now_et is None:
        return None

    # The snapshot may carry more roots than the strips use (gex_live reads
    # the full index book). The strips are PM-settled SPXW only — an
    # AM-settled SPX monthly landing on the same date must never mix in.
    allowed = set(p["roots"])
    quotes = [q for q in snapshot["quotes"] if q.get("root") in allowed]
    if not quotes:
        return None

    near, nxt = select_term_expiries(quotes, now_et, p, holidays)
    if near is None or nxt is None:
        print("[vol1d] proxy: need two live expiries, got near={} next={}".format(near, nxt))
        return None

    r = p["risk_free_rate"]
    terms = {}
    for label, exp in (("near", near), ("next", nxt)):
        t = daycount.business_time_to_expiry(
            now_et, exp, business_day_year=p["business_day_year"],
            settle_hour_et=p["settle_hour_et"], holidays=holidays)
        table = build_strike_table(
            [q for q in quotes if q["expiry"] == exp])
        tv = term_variance(table, r, t,
                           consecutive_no_bid_stop=p["consecutive_no_bid_stop"])
        if tv is None:
            print("[vol1d] proxy: {} strip unusable (expiry {})".format(label, exp))
            return None
        tv["t"] = t
        terms[label] = tv

    t1, t2 = terms["near"]["t"], terms["next"]["t"]
    if t2 <= t1:
        return None

    # Constant-maturity interpolation to the 1-business-day horizon. As T1
    # decays toward 0 through the session (and T2 toward the 1-day target),
    # w1 rolls off the 0DTE strip onto the next strip — the Cboe rolling
    # convention (near-term weight -> 0 at its expiry).
    tt = p["target_horizon_bd"] / p["business_day_year"]
    w1 = (t2 - tt) / (t2 - t1)
    w2 = (tt - t1) / (t2 - t1)

    total_var = w1 * t1 * terms["near"]["var"] + w2 * t2 * terms["next"]["var"]
    if total_var < 0:
        # Negative interpolated variance means a degenerate strip
        # (extrapolation weights + noisy quotes); refuse rather than emit NaN.
        print("[vol1d] proxy: negative interpolated variance")
        return None

    # Scale the 1-day variance back to annual terms: divide by the target
    # horizon (the business-time equivalent of the spec's N_365/N_1day).
    level = 100.0 * math.sqrt(total_var / tt)
    if not (p["level_lo"] <= level <= p["level_hi"]):
        print("[vol1d] proxy: level {:.1f} outside sanity bounds".format(level))
        return None

    return {
        "vix1d":        round(level, 3),
        "spot":         snapshot.get("spot"),
        "ts":           now_et,
        "near_expiry":  near.isoformat(),
        "next_expiry":  nxt.isoformat(),
        "t1":           t1,
        "t2":           t2,
        "w1":           round(w1, 4),
        "w2":           round(w2, 4),
        "sigma1_sq":    terms["near"]["var"],
        "sigma2_sq":    terms["next"]["var"],
        "f1":           round(terms["near"]["forward"], 2),
        "f2":           round(terms["next"]["forward"], 2),
        "k0_1":         terms["near"]["k0"],
        "k0_2":         terms["next"]["k0"],
        "n_strikes_1":  terms["near"]["n_strikes"],
        "n_strikes_2":  terms["next"]["n_strikes"],
        "source":       snapshot.get("source"),
    }
