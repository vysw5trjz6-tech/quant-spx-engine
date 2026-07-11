# vol1d/gex_live.py
# Intraday dealer-GEX from the same delayed CBOE chain the proxy reads.
#
# The nightly gex_bias freezes gamma as of the prior close; the profile
# actually migrates all session (spot crosses strikes, IV moves, 0DTE
# gamma concentrates). This recomputes the dealer book at live spot/IV by
# feeding the chain snapshot through gamma_exposure.compute_gex_from_chain
# — SAME formula, sign convention (calls +, puts -) and 0.5-day floor as
# the nightly build, so the two reads stay comparable.
#
# Honest limits, in the module because they matter:
#   * OI is a once-daily OCC print. This re-prices YESTERDAY'S book; the
#     0DTE positions opened today are invisible until tomorrow (only paid
#     trade-flow data sees them).
#   * Quotes are ~15 min delayed, like everything else off this CDN.
#   * This is INDEX (SPX/SPXW) GEX. Dollar-gamma magnitudes run far above
#     the SPY-chain numbers the nightly bias buckets with — hence the
#     separate neutral_band_b. Compare regimes, not raw $B, across the two.

from datetime import datetime

import pytz

import gamma_exposure
from vol1d import config as vol1d_config

ET = pytz.timezone("America/New_York")


def snapshot_to_gex_chain(snapshot, cfg_gex, today=None):
    """Map chain-source quotes onto the row shape
    gamma_exposure.compute_gex_from_chain consumes. Drops quotes without
    OI or IV, outside the root set, or past the expiry window."""
    roots = set(cfg_gex["roots"])
    max_ahead = cfg_gex["max_days_ahead"]
    if today is None:
        ts = snapshot.get("ts")
        today = ts.date() if ts else datetime.now(ET).date()

    rows = []
    for q in snapshot.get("quotes") or []:
        if q.get("root") not in roots:
            continue
        oi = q.get("open_interest") or 0
        iv = q.get("iv")
        if oi <= 0 or not iv or iv <= 0:
            continue
        # Defensive IV normalization: the CBOE payload quotes decimals
        # (0.18), but a percent-form feed (18.0) would silently zero the
        # whole read via _prep_contract's iv<=5 guard.
        if iv > 5.0:
            iv = iv / 100.0
        dte = (q["expiry"] - today).days
        if dte < 0 or dte > max_ahead:
            continue
        rows.append({
            "strike":             q["strike"],
            "expiry":             q["expiry"],
            "type":               q["type"],
            "open_interest":      oi,
            "implied_volatility": iv,
        })
    return rows


def compute_bias(snapshot, cfg=None, today=None):
    """gex_bias-shaped dict from a live chain snapshot, or None when the
    read is disabled/unusable. `regime` carries the nightly bias's
    vocabulary (LONG_GAMMA / SHORT_GAMMA / NEUTRAL) so vol1d.regime's
    map_gex_regime consumes either source unchanged."""
    cfg = cfg or vol1d_config.get_config()
    g = cfg["gex_live"]
    if not g["enabled"] or not snapshot or not snapshot.get("spot"):
        return None

    rows = snapshot_to_gex_chain(snapshot, g, today)
    if len(rows) < g["min_contracts"]:
        return None

    gex = gamma_exposure.compute_gex_from_chain(rows, snapshot["spot"])
    if not gex:
        return None

    total_b = gex["total_gex_billions"]
    regime = gex["regime"]
    if abs(total_b) < g["neutral_band_b"]:
        regime = "NEUTRAL"

    return {
        "regime":      regime,
        "gex_b":       total_b,
        "flip":        gex.get("zero_gamma_strike"),
        "call_wall":   gex.get("call_wall"),
        "put_wall":    gex.get("put_wall"),
        "spot":        gex.get("spot"),
        "computed_at": gex.get("computed_at"),
        "contracts":   len(rows),
        "source":      "cboe_delayed_live",
    }
