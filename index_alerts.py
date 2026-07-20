# index_alerts.py
# SPX / NDX denomination for the intraday alert path + the 10:30 AM
# intraday GEX update.
#
# The engine's DATA feeds stay on the liquid ETF proxies (Alpaca carries
# SPY/QQQ bars and ETF option chains only), but the user trades the cash
# INDEX options. This module converts a proxy-denominated signal into real
# index terms so every alert reads as the product actually being traded:
#
#   - underlying entry/stop/targets in index points, anchored on the real
#     prior index close moved by the proxy's return (main._index_anchor_level
#     supplies the ratio; the fixed multiplier is only the last-resort)
#   - a real index option contract: strike picked off the live delayed
#     SPX/NDX chain when reachable, else the index strike grid (SPXW 5-pt,
#     NDX 10-pt), with the premium taken from the chain mid or scaled from
#     the ETF premium as the estimate fallback
#
# Chain source: CBOE's free key-less delayed-quotes CDN — the same host
# vol1d already trusts for the SPX book, extended here with the _NDX.json
# endpoint. Free data, so neither the per-scan refinement nor the 10:30
# GEX update adds Databento spend.

import copy
import time
from datetime import datetime

import pytz

try:
    from vol1d import chain_source as _chain_source
    from vol1d import config as _vol1d_config
    from vol1d import gex_live as _gex_live
    _HAS_VOL1D = True
except ImportError:
    _HAS_VOL1D = False

ET = pytz.timezone("America/New_York")

PROXY_TO_INDEX = {"SPY": "SPX", "QQQ": "NDX"}

# Near-ATM strike spacing on the daily-expiry index chains.
STRIKE_STEP = {"SPX": 5.0, "NDX": 10.0}

# Root shown on the alert (where the daily/0DTE volume lives).
DISPLAY_ROOT = {"SPX": "SPXW", "NDX": "NDXP"}

# Delayed-quotes chain per index. One fetch carries both OPRA roots.
CHAIN_URLS = {
    "SPX": "https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json",
    "NDX": "https://cdn.cboe.com/api/global/delayed_quotes/options/_NDX.json",
}
CHAIN_ROOTS = {
    "SPX": ("SPXW", "SPX"),
    "NDX": ("NDXP", "NDX"),
}

# TTL cache on the chain fetch: the 5-min scan and the 10:30 update share
# snapshots instead of re-pulling the CDN for every signal row.
_CHAIN_TTL_SECS = 240
_chain_cache = {}   # index -> (fetched_at_epoch, snapshot_or_None)


# =============================================
# PURE TRANSLATION
# =============================================

def pick_strike(index, direction, idx_price):
    """First OTM strike on the index grid (ATM when price sits below/above
    the rounded strike) — same semantics as main.recommend_contract."""
    step = STRIKE_STEP.get(index)
    if not step or not idx_price:
        return None
    atm = round(round(idx_price / step) * step, 2)
    if direction == "CALL":
        return atm if idx_price < atm else round(atm + step, 2)
    if direction == "PUT":
        return atm if idx_price > atm else round(atm - step, 2)
    return None


def _pts(value, ratio):
    """Proxy dollars -> index points (None-safe)."""
    try:
        return round(float(value) * ratio, 2)
    except (TypeError, ValueError):
        return None


def translate_signal(sig, index, ratio, anchored=True):
    """
    Index-denominated view of a proxy signal row. Returns a dict of idx_*
    fields to merge into the row; empty dict when the inputs are unusable.

    ratio = index_level / proxy_price at signal time. Underlying levels and
    the option premium both scale ~linearly with the underlying for the
    same %-moneyness, so one ratio serves both (premium is flagged as an
    estimate until a live chain mid replaces it).
    """
    if not index or not ratio or ratio <= 0:
        return {}
    price = sig.get("price")
    idx_price = _pts(price, ratio)
    if not idx_price:
        return {}

    direction = sig.get("direction")
    out = {
        "idx_symbol":   index,
        "idx_ratio":    round(ratio, 6),
        "idx_anchored": bool(anchored),
        "idx_price":    idx_price,
    }

    # Direction-relevant underlying levels in index points.
    if direction in ("CALL", "PUT"):
        side = "call" if direction == "CALL" else "put"
        for tail in ("stop", "t1", "t2"):
            v = _pts(sig.get("und_{}_{}".format(side, tail)), ratio)
            if v is not None:
                out["idx_und_{}".format(tail)] = v
        out["idx_strike"] = pick_strike(index, direction, idx_price)
        out["idx_root"]   = DISPLAY_ROOT.get(index)

    # Premium view: scaled estimate; refine_with_chain() upgrades it to a
    # live chain mid when the delayed chain is reachable.
    prem = _pts(sig.get("premium"), ratio)
    if prem is not None:
        out["idx_premium"]     = prem
        out["idx_premium_src"] = "scaled_est"
        stop = _pts(sig.get("stop"), ratio)
        tgt  = _pts(sig.get("target"), ratio)
        if stop is not None:
            out["idx_opt_stop"] = stop
        if tgt is not None:
            out["idx_opt_target"] = tgt
    return out


def pick_contract(snapshot, direction, idx_price, today=None, max_steps=2):
    """
    Actual tradable contract off a delayed chain snapshot: front expiry on
    or after `today`, first strike at/beyond `idx_price` in the trade
    direction, walking up to `max_steps` strikes further OTM to find a
    quote with a live ask. Returns {"strike","expiry","dte","mid","bid",
    "ask","root"} or None.
    """
    if not snapshot or not idx_price or direction not in ("CALL", "PUT"):
        return None
    want = "call" if direction == "CALL" else "put"
    if today is None:
        ts = snapshot.get("ts")
        today = ts.date() if ts else datetime.now(ET).date()

    rows = [q for q in (snapshot.get("quotes") or [])
            if q.get("type") == want and q.get("expiry")
            and q["expiry"] >= today]
    if not rows:
        return None
    front = min(q["expiry"] for q in rows)
    front_rows = {}
    for q in rows:
        if q["expiry"] == front:
            front_rows.setdefault(q["strike"], q)

    if direction == "CALL":
        ladder = sorted(k for k in front_rows if k >= idx_price)
    else:
        ladder = sorted((k for k in front_rows if k <= idx_price),
                        reverse=True)
    if not ladder:
        return None

    for strike in ladder[:max_steps + 1]:
        q = front_rows[strike]
        bid = q.get("bid") or 0.0
        ask = q.get("ask") or 0.0
        if ask > 0:
            return {
                "strike": strike,
                "expiry": front.isoformat(),
                "dte":    (front - today).days,
                "mid":    round((bid + ask) / 2.0, 2),
                "bid":    bid,
                "ask":    ask,
                "root":   q.get("root"),
            }
    return None


def refine_with_chain(idx_fields, snapshot, direction, today=None):
    """Upgrade a translate_signal() dict in place with the real chain
    contract (strike + mid premium + premium exits) when available."""
    if not idx_fields or not snapshot:
        return idx_fields
    c = pick_contract(snapshot, direction, idx_fields.get("idx_price"),
                      today=today)
    if not c:
        return idx_fields
    idx_fields["idx_strike"]      = c["strike"]
    idx_fields["idx_root"]        = c.get("root") or idx_fields.get("idx_root")
    idx_fields["idx_expiry"]      = c["expiry"]
    idx_fields["idx_dte"]         = c["dte"]
    if c.get("mid"):
        idx_fields["idx_premium"]     = c["mid"]
        idx_fields["idx_premium_src"] = "chain_mid"
        # Same premium-exit fractions main.option_risk_levels applies.
        idx_fields["idx_opt_stop"]    = round(c["mid"] * 0.55, 2)
        idx_fields["idx_opt_target"]  = round(c["mid"] * 1.4, 2)
    return idx_fields


# =============================================
# DELAYED CHAIN FETCH (shared, TTL-cached)
# =============================================

def fetch_chain(index, ttl_secs=_CHAIN_TTL_SECS, force=False):
    """Delayed chain snapshot for 'SPX' | 'NDX', TTL-cached. None on any
    failure (cached too, so a dead CDN isn't re-polled every signal row)."""
    if not _HAS_VOL1D:
        return None
    index = (index or "").upper()
    url = CHAIN_URLS.get(index)
    if not url:
        return None
    now = time.time()
    if not force:
        hit = _chain_cache.get(index)
        if hit and now - hit[0] < ttl_secs:
            return hit[1]
    snap = None
    try:
        src = _chain_source.CboeDelayedChainSource(
            url=url, roots=CHAIN_ROOTS[index])
        snap = src.get_snapshot()
    except Exception as e:
        print("[index_alerts] {} chain fetch failed: {}".format(index, e))
    _chain_cache[index] = (now, snap)
    return snap


# =============================================
# INTRADAY INDEX GEX (10:30 update)
# =============================================

def compute_index_gex(index, snapshot=None, cfg=None):
    """
    Live dealer-GEX read for a cash index off the delayed chain, riding
    vol1d.gex_live's exact formula/sign convention. Returns the bias dict
    (regime / gex_b / flip / walls / spot) with "index" added, or None.
    """
    if not _HAS_VOL1D:
        return None
    index = (index or "").upper()
    if snapshot is None:
        snapshot = fetch_chain(index)
    if not snapshot:
        return None
    cfg = copy.deepcopy(cfg or _vol1d_config.get_config())
    cfg["gex_live"]["roots"] = list(CHAIN_ROOTS.get(index, ()))
    bias = _gex_live.compute_bias(snapshot, cfg=cfg)
    if bias:
        bias["index"] = index
    return bias


def _fmt_pts(v):
    if v is None:
        return "?"
    try:
        return "{:,.0f}".format(float(v))
    except (TypeError, ValueError):
        return str(v)


def build_gex_update_message(reads, baselines=None, now_et=None):
    """
    Telegram text for the intraday GEX update, 1h into the session.

    reads:     {"SPX": bias_dict_or_None, "NDX": ...} from compute_index_gex
    baselines: {"SPX": index_insights_dict_or_None, ...} — the premarket
               index-chain read (EOD OI + settlement) this update revises.
    Returns None when no index produced a live read (nothing to send).
    """
    baselines = baselines or {}
    if now_et is None:
        now_et = datetime.now(ET)

    lines = ["🔄 GEX UPDATE — {} ET (1h into session)".format(
        now_et.strftime("%H:%M"))]
    got_read = False

    for idx in ("SPX", "NDX"):
        r = reads.get(idx)
        lines.append("")
        if not r:
            lines.append("{}: no live chain read".format(idx))
            continue
        got_read = True
        base = baselines.get(idx) or {}

        spot = r.get("spot")
        lines.append("{} {}".format(idx, _fmt_pts(spot)))

        regime = r.get("regime", "?")
        shift = ""
        base_regime = base.get("gex_regime")
        if base_regime:
            shift = ("  (was {} premkt)".format(base_regime)
                     if base_regime != regime else "  (unchanged from premkt)")
        lines.append("  GEX: ${}B {}{}".format(r.get("gex_b", "?"),
                                               regime, shift))

        flip = r.get("flip")
        if flip and spot:
            side = "ABOVE" if spot > flip else "BELOW"
            tape = ("dip-buy / pin tape" if side == "ABOVE"
                    else "trend / accelerant tape")
            lines.append("  Zero-gamma: {} (spot {} — {})".format(
                _fmt_pts(flip), side, tape))
            base_flip = base.get("zero_gamma")
            if base_flip and abs(flip - base_flip) >= STRIKE_STEP.get(idx, 5.0):
                lines.append("  Flip moved {}{} pts since premkt ({})".format(
                    "+" if flip > base_flip else "-",
                    _fmt_pts(abs(flip - base_flip)), _fmt_pts(base_flip)))
        elif flip:
            lines.append("  Zero-gamma: {}".format(_fmt_pts(flip)))

        cw, pw = r.get("call_wall"), r.get("put_wall")
        if cw or pw:
            lines.append("  Call wall {} | Put wall {}".format(
                _fmt_pts(cw), _fmt_pts(pw)))

    if not got_read:
        return None

    lines.append("")
    lines.append("OI = last night's book repriced at live spot/IV "
                 "(CBOE delayed, free feed).")
    return "\n".join(lines)
