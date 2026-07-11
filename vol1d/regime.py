# vol1d/regime.py
# Vol-state classification + the GEX-cross grid (spec §3).
#
# The classifier keys off vix1d_tod_z — the DETRENDED level — never the raw
# level (see vol1d/baseline.py). Hysteresis: the emitted label only flips
# after the raw state persists for regime.hysteresis_bars consecutive
# updates; wide bands + hysteresis beat precise-but-fragile cutoffs.
#
# The 2-factor grid against dealer GEX is where the real gating lives:
#
#                     Positive GEX (dampen)   Negative GEX (amplify)
#   COMPRESSED        FADE (highest conviction) MIXED (two-sided swings)
#   EXPANSIVE         REDUCE (transitional)     TREND (continuation)
#
# Corners are the tradeable signal; diagonals are reduce-size/stand-aside.
# The engine's GEX snapshot is built nightly from EOD OI, so the grid
# crosses a live vol state against a prior-close mechanism read — the
# state layer reflects that staleness in `confidence`, not here.

COMPRESSED = "COMPRESSED"
NEUTRAL    = "NEUTRAL"
EXPANSIVE  = "EXPANSIVE"

POS_GEX     = "POS_GEX"
NEG_GEX     = "NEG_GEX"
UNKNOWN_GEX = "UNKNOWN_GEX"

# grid_action values the ruleset consumes (shadow-only until vol1d.enforce)
FADE        = "FADE"
TREND       = "TREND"
MIXED       = "MIXED"
REDUCE      = "REDUCE"
STAND_ASIDE = "STAND_ASIDE"


def raw_vol_state(tod_z, iv_rv_spread, cfg_regime):
    """Instantaneous (pre-hysteresis) state per the spec pseudocode.
    Warmup (tod_z None) reads NEUTRAL — no baseline, no opinion."""
    if tod_z is None:
        return NEUTRAL
    spread = iv_rv_spread
    if tod_z <= cfg_regime["low_z"] and spread is not None and spread > 0:
        return COMPRESSED          # IV rich, RV quiet -> fade/mean-revert
    if tod_z >= cfg_regime["high_z"] or (spread is not None and spread < 0):
        return EXPANSIVE           # RV catching/exceeding IV -> trend/expand
    return NEUTRAL


def map_gex_regime(gex_bias):
    """Collapse gamma_exposure.get_gex_bias() output onto the grid's GEX
    axis. NEUTRAL GEX (|GEX| < 1B) carries no dealer mechanism either way,
    so it maps to UNKNOWN_GEX rather than picking a column."""
    regime = (gex_bias or {}).get("regime")
    if regime == "LONG_GAMMA":
        return POS_GEX
    if regime == "SHORT_GAMMA":
        return NEG_GEX
    return UNKNOWN_GEX


_GRID = {
    (COMPRESSED, POS_GEX): FADE,
    (EXPANSIVE,  NEG_GEX): TREND,
    (COMPRESSED, NEG_GEX): MIXED,
    (EXPANSIVE,  POS_GEX): REDUCE,
}


def combined_regime(vol_state, gex_label):
    """(combined_regime_str, grid_action). NEUTRAL vol or unknown GEX
    yields STAND_ASIDE — no corner, no conviction."""
    action = _GRID.get((vol_state, gex_label), STAND_ASIDE)
    return "{}/{}".format(vol_state, gex_label), action


class RegimeTracker(object):
    """Hysteresis wrapper the updater thread owns. Feed it every pass."""

    def __init__(self, cfg_regime):
        self.cfg = cfg_regime
        self._emitted = NEUTRAL
        self._pending = None
        self._pending_count = 0

    def update(self, tod_z, iv_rv_spread, vix1d_roc, gex_bias=None):
        """Returns {vol_state, raw_state, spiking, combined_regime,
        grid_action}."""
        raw = raw_vol_state(tod_z, iv_rv_spread, self.cfg)

        if raw == self._emitted:
            self._pending = None
            self._pending_count = 0
        elif raw == self._pending:
            self._pending_count += 1
            if self._pending_count >= self.cfg["hysteresis_bars"]:
                self._emitted = raw
                self._pending = None
                self._pending_count = 0
        else:
            self._pending = raw
            self._pending_count = 1

        spiking = (vix1d_roc is not None
                   and vix1d_roc >= self.cfg["roc_spike_thresh"])

        gex_label = map_gex_regime(gex_bias)
        combined, action = combined_regime(self._emitted, gex_label)
        return {
            "vol_state":       self._emitted,
            "raw_state":       raw,
            "spiking":         spiking,
            "gex_label":       gex_label,
            "combined_regime": combined,
            "grid_action":     action,
        }
