# index_options.py
# SPX / NDX index-options insights for the premarket brief.
#
# The engine already builds SPY/QQQ dealer GEX for the scanner. This module
# reads the ACTUAL index option chains (the products 0DTE flow actually
# trades) and distills the numbers an intraday index trader wants at 9:10 AM,
# all in real index points:
#
#   - 0DTE expected move: today's-expiry ATM straddle from last settlement
#   - Dealer gamma walls: call wall / put wall / zero-gamma flip strike
#   - Put/call open-interest ratio on the front expiry
#   - Top OI strikes on the front expiry (the day's magnet levels)
#
# Chain source: Databento OPRA statistics (EOD OI + settlement prices), the
# same cost-guarded path the SPY/QQQ GEX snapshot uses. Index options split
# across OPRA roots, so we pull both roots per index in one request.
#
# Everything is as-of the prior close: OI is EOD-published and settlement
# prices are yesterday's, so the expected move is "what the market priced
# last night", not a live quote. That is exactly the premarket read we want.

import math
from datetime import datetime

import pytz

try:
    import gamma_exposure
    _HAS_GEX = True
except ImportError:
    _HAS_GEX = False

# OPRA roots per cash index. SPX = monthly AM-settled, SPXW = weekly/daily
# PM-settled (where all the 0DTE volume lives). Same split for NDX/NDXP.
INDEX_OPTION_ROOTS = {
    "SPX": ["SPX", "SPXW"],
    "NDX": ["NDX", "NDXP"],
}


def _front_expiry(chain, today_iso):
    """Nearest expiry on/after today, or None."""
    future = sorted({c["expiry"] for c in chain if c["expiry"] >= today_iso})
    return future[0] if future else None


def _best_px(rows):
    """
    One price per (strike, type) on an expiry. AM- and PM-settled roots can
    list the same strike/date; prefer the contract with the larger OI (the
    one the market actually trades).
    """
    best = None
    for r in rows:
        if r.get("price") is None:
            continue
        if best is None or (r.get("open_interest") or 0) > (best.get("open_interest") or 0):
            best = r
    return best


def compute_insights_from_chain(chain, spot, index=None, today=None):
    """
    Pure computation over a chain snapshot (list of {strike, expiry, type,
    open_interest, price, ...}) at index level `spot`. Returns the insights
    dict, or None when the chain/spot is unusable. Offline-testable.
    """
    if not chain or not spot or spot <= 0:
        return None

    et = pytz.timezone("America/New_York")
    if today is None:
        today = datetime.now(et).date()
    today_iso = today.isoformat()

    front = _front_expiry(chain, today_iso)
    if not front:
        return None
    front_rows = [c for c in chain if c["expiry"] == front]

    # --- 0DTE expected move from the ATM straddle -------------------------
    em_pts = em_pct = atm_strike = None
    by_strike = {}
    for c in front_rows:
        by_strike.setdefault(c["strike"], {"call": [], "put": []})
        if c["type"] in ("call", "put"):
            by_strike[c["strike"]][c["type"]].append(c)
    # Nearest-to-spot strike that has BOTH legs priced.
    for strike in sorted(by_strike, key=lambda k: abs(k - spot)):
        call = _best_px(by_strike[strike]["call"])
        put  = _best_px(by_strike[strike]["put"])
        if call and put:
            atm_strike = strike
            em_pts = float(call["price"]) + float(put["price"])
            em_pct = em_pts / spot * 100.0
            break

    # --- Dealer gamma walls over the pulled expiries ----------------------
    call_wall = put_wall = zero_gamma = gex_b = gex_regime = None
    if _HAS_GEX:
        gex = gamma_exposure.compute_gex_from_chain(chain, spot)
        if gex:
            call_wall  = gex.get("call_wall")
            put_wall   = gex.get("put_wall")
            zero_gamma = gex.get("zero_gamma_strike")
            gex_b      = gex.get("total_gex_billions")
            gex_regime = gex.get("regime")

    # --- Front-expiry OI structure ----------------------------------------
    call_oi = sum(c.get("open_interest") or 0
                  for c in front_rows if c["type"] == "call")
    put_oi  = sum(c.get("open_interest") or 0
                  for c in front_rows if c["type"] == "put")
    pc_oi = round(put_oi / call_oi, 2) if call_oi else None

    def _top(side, n=3):
        agg = {}
        for c in front_rows:
            if c["type"] != side:
                continue
            agg[c["strike"]] = agg.get(c["strike"], 0) + (c.get("open_interest") or 0)
        top = sorted(agg.items(), key=lambda kv: -kv[1])[:n]
        return [[k, v] for k, v in top if v > 0]

    dte = (datetime.strptime(front, "%Y-%m-%d").date() - today).days

    return {
        "index":        index,
        "spot":         round(float(spot), 2),
        "front_expiry": front,
        "dte":          dte,
        "is_0dte":      dte == 0,
        "atm_strike":   atm_strike,
        "expected_move_pts": round(em_pts, 1) if em_pts is not None else None,
        "expected_move_pct": round(em_pct, 2) if em_pct is not None else None,
        "em_low":  round(spot - em_pts, 1) if em_pts is not None else None,
        "em_high": round(spot + em_pts, 1) if em_pts is not None else None,
        "call_wall":    call_wall,
        "put_wall":     put_wall,
        "zero_gamma":   zero_gamma,
        "gex_b":        gex_b,
        "gex_regime":   gex_regime,
        "pc_oi":        pc_oi,
        "front_call_oi": call_oi,
        "front_put_oi":  put_oi,
        "top_call_oi":  _top("call"),
        "top_put_oi":   _top("put"),
        "contracts":    len(chain),
        "note": "EOD OI + settlement prices (prior close)",
    }


def get_index_options_insights(index, spot, expiries_ahead=4):
    """
    Fetch the real index option chain and compute premarket insights.

    index: 'SPX' | 'NDX'. spot: current index level in index points (the
    implied open from the futures ratio is the best premarket choice).
    Returns the insights dict or None (Databento unavailable, empty chain).
    """
    index = index.upper()
    roots = INDEX_OPTION_ROOTS.get(index)
    if not roots or not spot:
        return None
    try:
        import databento_adapter
        if not databento_adapter.is_available():
            return None
        chain = databento_adapter.get_options_chain_snapshot(
            index, with_price=True, expiries_ahead=expiries_ahead,
            roots=roots)
    except ImportError:
        return None
    except Exception:
        return None
    if not chain:
        return None
    return compute_insights_from_chain(chain, spot, index=index)
