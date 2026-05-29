"""Volume-vs-open-interest confluence for the weekly options pull.

The classic "unusual options activity" tell is daily contract VOLUME running hot
versus existing OPEN INTEREST (volume/OI >= ~1 means more contracts traded today
than were open -> fresh positioning, not just churn). We source the two sides
from DIFFERENT feeds and cross-confirm:

  * volume      -- primarily Alpaca (the picked contract's dailyBar volume),
                   corroborated by Databento ohlcv-1d volume when present.
  * open_interest -- Databento OPRA statistics (already stored in oi_delta.db
                   for SPY/QQQ; fetched on demand + cached for weekly stocks).

A flag is only treated as a real confluence factor when BOTH feeds contribute
(Databento OI present AND a volume reading present) -- a one-sided number is
noise. Output feeds the signal score as bonus points and is shown on the card.

Pure-ish: imports oi_delta (DB read) and, optionally, databento_adapter for an
on-demand OI fetch gated by the caller.
"""

try:
    import oi_delta
    _HAS_OI = True
except Exception:  # pragma: no cover
    _HAS_OI = False

try:
    import databento_adapter
    _HAS_DBN = True
except Exception:  # pragma: no cover
    _HAS_DBN = False


# Tuning. Ratios are daily option volume / open interest for the picked strike.
ELEVATED_RATIO = 0.5
UNUSUAL_RATIO  = 1.0
ELEVATED_PTS   = 6
UNUSUAL_PTS    = 12


def _volumes_agree(a, b):
    """True if two volume readings corroborate (within ~2x of each other)."""
    if not a or not b:
        return None          # can't compare -> unknown
    lo, hi = min(a, b), max(a, b)
    return (lo / hi) >= 0.5


def _maybe_fetch_oi(symbol):
    """On-demand cheap OI sweep for a symbol not in the daily pipeline.

    Uses the Databento OI snapshot (cached ~1h in databento_cache.db) and
    persists it to oi_delta so the per-contract reader can find it. Gated by the
    caller (only worth it for the handful of tier-1 weekly setups/day).
    """
    if not (_HAS_DBN and _HAS_OI):
        return
    try:
        if hasattr(databento_adapter, "is_available") and not databento_adapter.is_available():
            return
        chain = databento_adapter.get_options_chain_snapshot(symbol)
        if chain:
            oi_delta.save_snapshot(symbol, chain)
    except Exception:
        pass


def compute_vol_oi(symbol, strike, expiry, opt_type, alpaca_volume=None,
                   allow_fetch=False):
    """Compute the vol/OI confluence for one contract.

    opt_type: "call" | "put". alpaca_volume: the picked contract's Alpaca daily
    volume (preferred volume source). allow_fetch: permit a Databento OI sweep
    when none is stored (use only for tier-1 weekly setups -- it costs a
    request).

    Returns a dict (never raises):
      {oi, volume, alpaca_volume, dbn_volume, ratio, flag, confirmed,
       volume_agree, points, note}
    flag in {None, "NORMAL", "ELEVATED", "UNUSUAL"}.
    """
    out = {"oi": None, "volume": None, "alpaca_volume": alpaca_volume,
           "dbn_volume": None, "ratio": None, "flag": None,
           "confirmed": False, "volume_agree": None, "points": 0, "note": ""}
    if not _HAS_OI or strike is None or expiry is None or opt_type is None:
        return out

    src = oi_delta.get_contract_oi_vol(symbol, strike, expiry, opt_type)
    if (src.get("oi") is None) and allow_fetch:
        _maybe_fetch_oi(symbol)
        src = oi_delta.get_contract_oi_vol(symbol, strike, expiry, opt_type)

    oi        = src.get("oi")
    dbn_vol   = src.get("volume")
    out["oi"] = oi
    out["dbn_volume"] = dbn_vol

    volume = alpaca_volume if alpaca_volume is not None else dbn_vol
    out["volume"] = volume
    out["volume_agree"] = _volumes_agree(alpaca_volume, dbn_vol)

    if not oi or not volume:
        out["note"] = "insufficient data (oi={}, vol={})".format(oi, volume)
        return out

    ratio = round(volume / oi, 2)
    out["ratio"] = ratio
    # Both feeds contributed (Databento OI + a volume reading) -> trustworthy.
    out["confirmed"] = True

    if ratio >= UNUSUAL_RATIO:
        out["flag"] = "UNUSUAL"
        out["points"] = UNUSUAL_PTS
    elif ratio >= ELEVATED_RATIO:
        out["flag"] = "ELEVATED"
        out["points"] = ELEVATED_PTS
    else:
        out["flag"] = "NORMAL"
        out["points"] = 0

    # If the two feeds' volumes disagree wildly, soften the bonus (still a
    # signal, but lower confidence).
    if out["volume_agree"] is False and out["points"] > 0:
        out["points"] = max(3, out["points"] // 2)

    out["note"] = "vol {} / OI {} = {} ({})".format(
        volume, oi, ratio, out["flag"])
    return out
