# Offline tests for the VIX1D proxy: forward/K0, the OTM strike-selection
# stop rule, deltaK edge handling, and an end-to-end synthetic-chain
# recovery check (flat-vol Black-Scholes chain -> proxy must read back the
# input vol). No network, no Databento.

import math
from datetime import date, datetime

import vol_math
from vol1d import config as vol1d_config
from vol1d import chain_source, daycount, proxy


NOW    = datetime(2026, 7, 8, 9, 30)     # Wednesday 09:30 ET
TODAY  = date(2026, 7, 8)                # 0DTE expiry
NEXT   = date(2026, 7, 9)                # 1DTE expiry
SPOT   = 6300.0
FLAT_VOL = 0.20


def _cfg(r=0.0):
    cfg = vol1d_config.get_config()
    cfg["proxy"]["risk_free_rate"] = r
    return cfg


def _t(expiry):
    return daycount.business_time_to_expiry(NOW, expiry)


def _synthetic_quotes(expiry, sigma=FLAT_VOL, spot=SPOT, width=0.06, step=5.0):
    """Flat-vol BS chain around spot: both sides quoted at every strike,
    bid/ask a tight band around the model mid. Deep-OTM strikes whose model
    price collapses to ~0 get a zero bid (as on a real board)."""
    t = _t(expiry)
    quotes = []
    k = spot * (1 - width)
    k = math.floor(k / step) * step
    while k <= spot * (1 + width):
        for typ in ("call", "put"):
            mid = vol_math.bs_price(spot, k, t, 0.0, sigma, typ)
            if mid >= 0.05:
                quotes.append({"root": "SPXW", "expiry": expiry, "type": typ,
                               "strike": k, "bid": mid * 0.99, "ask": mid * 1.01})
            else:
                quotes.append({"root": "SPXW", "expiry": expiry, "type": typ,
                               "strike": k, "bid": 0.0, "ask": 0.05})
        k += step
    return quotes


def _snapshot(quotes, spot=SPOT):
    return {"ts": NOW, "spot": spot, "quotes": quotes, "source": "test"}


# ---------------------------------------------------------------------------
# Forward / K0
# ---------------------------------------------------------------------------

def test_forward_and_k0_from_parity():
    # With r=0, F = K_atm + (C - P). Build mids so C-P is smallest at 6300
    # and equals +2.5 there -> F = 6302.5, K0 = first strike BELOW F = 6300.
    table = {
        6295.0: {"call": (9.0, 11.0), "put": (2.0, 4.0)},    # C-P = +7
        6300.0: {"call": (6.0, 8.0),  "put": (3.5, 5.5)},    # C-P = +2.5 (min)
        6305.0: {"call": (3.0, 5.0),  "put": (9.0, 11.0)},   # C-P = -6
    }
    f, k0 = proxy.forward_and_k0(table, r=0.0, t=1.0 / 252)
    assert abs(f - 6302.5) < 1e-9
    assert k0 == 6300.0


def test_k0_strictly_below_forward():
    # If F lands exactly on a strike, K0 is the strike BELOW it (spec: the
    # first strike below F, not at-or-below).
    table = {
        6295.0: {"call": (8.0, 8.0), "put": (3.0, 3.0)},
        6300.0: {"call": (5.0, 5.0), "put": (5.0, 5.0)},     # C-P = 0 -> F = 6300
    }
    f, k0 = proxy.forward_and_k0(table, r=0.0, t=1.0 / 252)
    assert f == 6300.0
    assert k0 == 6295.0


def test_forward_requires_both_legs_bid():
    table = {6300.0: {"call": (5.0, 6.0), "put": (0.0, 1.0)}}   # put no-bid
    f, k0 = proxy.forward_and_k0(table, r=0.0, t=1.0 / 252)
    assert f is None and k0 is None


# ---------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------

def _one_sided_table(rows):
    """rows: [(strike, put_bid)] for the put side below K0=6300, plus K0."""
    table = {6300.0: {"call": (5.0, 6.0), "put": (5.0, 6.0)}}
    for k, bid in rows:
        table[k] = {"put": (bid, bid + 1.0 if bid > 0 else 0.5)}
    return table


def test_two_consecutive_no_bid_stops_the_walk():
    table = _one_sided_table([
        (6295.0, 4.0),
        (6290.0, 0.0),   # miss 1
        (6285.0, 0.0),   # miss 2 -> stop
        (6280.0, 3.0),   # bid, but beyond the stop: excluded
    ])
    selected = proxy.select_otm_quotes(table, 6300.0, consecutive_no_bid_stop=2)
    strikes = [k for k, _ in selected]
    assert 6280.0 not in strikes
    assert 6295.0 in strikes


def test_single_no_bid_is_dropped_but_walk_continues():
    table = _one_sided_table([
        (6295.0, 4.0),
        (6290.0, 0.0),   # miss 1 (dropped)
        (6285.0, 3.0),   # bid again -> counter resets, still included
    ])
    selected = proxy.select_otm_quotes(table, 6300.0, consecutive_no_bid_stop=2)
    strikes = [k for k, _ in selected]
    assert 6285.0 in strikes
    assert 6290.0 not in strikes


def test_k0_quote_averages_call_and_put():
    table = {6300.0: {"call": (6.0, 8.0), "put": (3.0, 5.0)}}   # mids 7 and 4
    selected = proxy.select_otm_quotes(table, 6300.0)
    assert selected == [(6300.0, 5.5)]


# ---------------------------------------------------------------------------
# deltaK edges
# ---------------------------------------------------------------------------

def test_delta_k_interior_and_edges():
    strikes = [6280.0, 6290.0, 6300.0, 6320.0]
    assert proxy.delta_k(strikes, 0) == 10.0            # low edge: single neighbor
    assert proxy.delta_k(strikes, 1) == 10.0            # (6300-6280)/2
    assert proxy.delta_k(strikes, 2) == 15.0            # (6320-6290)/2
    assert proxy.delta_k(strikes, 3) == 20.0            # high edge


# ---------------------------------------------------------------------------
# End-to-end: synthetic flat-vol chain must read back the input vol
# ---------------------------------------------------------------------------

def test_proxy_recovers_flat_vol_from_synthetic_chain():
    quotes = _synthetic_quotes(TODAY) + _synthetic_quotes(NEXT)
    out = proxy.compute_vix1d(_snapshot(quotes), now_et=NOW, cfg=_cfg(r=0.0))
    assert out is not None
    # Variance-swap reconstruction of a flat-vol surface, discretized to
    # 5-pt strikes with truncated wings, lands within ~0.5 vol pt.
    assert abs(out["vix1d"] - FLAT_VOL * 100) < 0.5
    assert out["near_expiry"] == TODAY.isoformat()
    assert out["next_expiry"] == NEXT.isoformat()
    # Morning weights: 0DTE has ~0.27 business days left, so the 1-day
    # constant-maturity read leans on the next-term strip.
    assert 0 < out["w1"] < 0.5
    assert abs(out["w1"] + out["w2"] - 1.0) < 1e-9


def test_proxy_weight_rolls_off_near_strip_into_the_close():
    # Late session (15:30): T1 -> 0, so w1 must be near zero (Cboe rolling
    # convention: near-term weight -> 0 at its expiry).
    late = datetime(2026, 7, 8, 15, 30)

    def _q(expiry):
        t = daycount.business_time_to_expiry(late, expiry)
        qs = []
        for k in range(int(SPOT * 0.97), int(SPOT * 1.03), 5):
            for typ in ("call", "put"):
                mid = vol_math.bs_price(SPOT, float(k), t, 0.0, FLAT_VOL, typ)
                bid = mid * 0.99 if mid >= 0.05 else 0.0
                qs.append({"root": "SPXW", "expiry": expiry, "type": typ,
                           "strike": float(k), "bid": bid,
                           "ask": mid * 1.01 if mid >= 0.05 else 0.05})
        return qs

    out = proxy.compute_vix1d(
        {"ts": late, "spot": SPOT, "quotes": _q(TODAY) + _q(NEXT), "source": "test"},
        now_et=late, cfg=_cfg(r=0.0))
    assert out is not None
    assert out["w1"] < 0.05
    assert abs(out["vix1d"] - FLAT_VOL * 100) < 0.6


def test_near_strip_rolls_when_under_min_t1():
    # 15:56 with min_t1_minutes=5 -> today's strip (4 min left) is dropped;
    # near-term becomes tomorrow.
    at = datetime(2026, 7, 8, 15, 56)
    quotes = [{"root": "SPXW", "expiry": e, "type": "call",
               "strike": 6300.0, "bid": 1.0, "ask": 1.2}
              for e in (TODAY, NEXT, date(2026, 7, 10))]
    cfg = _cfg()
    near, nxt = proxy.select_term_expiries(quotes, at, cfg["proxy"])
    assert near == NEXT
    assert nxt == date(2026, 7, 10)


def test_proxy_refuses_single_expiry():
    quotes = _synthetic_quotes(TODAY)
    assert proxy.compute_vix1d(_snapshot(quotes), now_et=NOW, cfg=_cfg(r=0.0)) is None


def test_spx_root_quotes_never_enter_the_strips():
    # The snapshot now carries the SPX root for the live-GEX consumer. An
    # AM-settled SPX monthly landing on the strip dates must not move the
    # PM-settled SPXW calc — garbage SPX quotes at the same expiries must
    # leave the level unchanged.
    quotes = _synthetic_quotes(TODAY) + _synthetic_quotes(NEXT)
    clean = proxy.compute_vix1d(_snapshot(quotes), now_et=NOW, cfg=_cfg(r=0.0))
    polluted = quotes + [
        {"root": "SPX", "expiry": e, "type": t, "strike": 6300.0,
         "bid": 500.0, "ask": 600.0}
        for e in (TODAY, NEXT) for t in ("call", "put")
    ]
    out = proxy.compute_vix1d(_snapshot(polluted), now_et=NOW, cfg=_cfg(r=0.0))
    assert out["vix1d"] == clean["vix1d"]


# ---------------------------------------------------------------------------
# Chain source payload parsing (offline)
# ---------------------------------------------------------------------------

def test_cboe_payload_parse_filters_roots_and_keeps_zero_bids():
    payload = {"data": {
        "current_price": 6301.2,
        "options": [
            {"option": "SPXW260708C06300000", "bid": 5.1, "ask": 5.9},
            {"option": "SPXW260708P06300000", "bid": 0.0, "ask": 0.1},  # kept: proxy must SEE no-bids
            {"option": "SPX260918C06300000",  "bid": 99.0, "ask": 101.0},  # SPX root: filtered
            {"option": "GARBAGE",             "bid": 1.0, "ask": 2.0},
        ],
    }}
    src = chain_source.CboeDelayedChainSource(roots=["SPXW"])
    snap = src.parse_payload(payload)
    assert snap is not None
    assert snap["spot"] == 6301.2
    assert len(snap["quotes"]) == 2
    zero_bid = [q for q in snap["quotes"] if q["bid"] == 0.0]
    assert len(zero_bid) == 1 and zero_bid[0]["type"] == "put"


def test_option_symbol_parse():
    p = chain_source.parse_option_symbol("SPXW260708P06250000")
    assert p == {"root": "SPXW", "expiry": date(2026, 7, 8),
                 "type": "put", "strike": 6250.0}
    assert chain_source.parse_option_symbol("NOT_AN_OPTION") is None


def test_log_throttled_dedupes_within_window(capsys):
    # Identical diagnostics fired on the ~15s updater cadence must print once
    # per throttle window, not on every pass (the market-open log flood).
    proxy._last_logged.clear()
    for _ in range(20):
        proxy._log_throttled("[vol1d] proxy: near strip unusable (expiry X)")
    out = capsys.readouterr().out
    assert out.count("near strip unusable") == 1

    # A distinct message is not suppressed by another's throttle entry.
    proxy._log_throttled("[vol1d] proxy: negative interpolated variance")
    out = capsys.readouterr().out
    assert out.count("negative interpolated variance") == 1
    proxy._last_logged.clear()
