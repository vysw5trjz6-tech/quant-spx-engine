# Offline tests for vol1d/regime.py: threshold logic on the DETRENDED
# level, hysteresis flip behavior, the spike overlay, and the GEX-cross
# grid mapping.

from vol1d import regime


CFG = {"low_z": -0.75, "high_z": 0.75, "roc_spike_thresh": 1.5,
       "roc_window_min": 10, "hysteresis_bars": 3}


# ---------------------------------------------------------------------------
# Raw state thresholds
# ---------------------------------------------------------------------------

def test_compressed_needs_low_z_AND_positive_spread():
    assert regime.raw_vol_state(-1.0, 2.0, CFG) == regime.COMPRESSED
    # Low z but RV already at/over IV -> not compressed.
    assert regime.raw_vol_state(-1.0, -0.5, CFG) == regime.EXPANSIVE
    assert regime.raw_vol_state(-1.0, None, CFG) == regime.NEUTRAL


def test_expansive_on_high_z_OR_negative_spread():
    assert regime.raw_vol_state(1.0, 2.0, CFG) == regime.EXPANSIVE
    assert regime.raw_vol_state(0.0, -0.1, CFG) == regime.EXPANSIVE


def test_neutral_band_and_warmup():
    assert regime.raw_vol_state(0.0, 1.0, CFG) == regime.NEUTRAL
    assert regime.raw_vol_state(None, 5.0, CFG) == regime.NEUTRAL   # warmup


# ---------------------------------------------------------------------------
# Hysteresis
# ---------------------------------------------------------------------------

def test_label_flips_only_after_hysteresis_bars():
    tr = regime.RegimeTracker(dict(CFG))
    # Two expansive reads: not enough (hysteresis_bars=3).
    for _ in range(2):
        out = tr.update(1.5, 1.0, 0.0)
    assert out["vol_state"] == regime.NEUTRAL
    assert out["raw_state"] == regime.EXPANSIVE
    # Third consecutive read flips the emitted label.
    out = tr.update(1.5, 1.0, 0.0)
    assert out["vol_state"] == regime.EXPANSIVE


def test_interrupted_streak_resets_counter():
    tr = regime.RegimeTracker(dict(CFG))
    tr.update(1.5, 1.0, 0.0)
    tr.update(1.5, 1.0, 0.0)
    tr.update(0.0, 1.0, 0.0)          # back to neutral: streak broken
    out = tr.update(1.5, 1.0, 0.0)
    out = tr.update(1.5, 1.0, 0.0)
    assert out["vol_state"] == regime.NEUTRAL   # only 2 since the reset


def test_flip_between_extremes_passes_through_hysteresis():
    tr = regime.RegimeTracker(dict(CFG))
    for _ in range(3):
        tr.update(-1.5, 1.0, 0.0)
    assert tr.update(-1.5, 1.0, 0.0)["vol_state"] == regime.COMPRESSED
    for _ in range(2):
        out = tr.update(1.5, 1.0, 0.0)
    assert out["vol_state"] == regime.COMPRESSED   # still holding
    assert tr.update(1.5, 1.0, 0.0)["vol_state"] == regime.EXPANSIVE


# ---------------------------------------------------------------------------
# Spike overlay
# ---------------------------------------------------------------------------

def test_spiking_overlay_is_immediate_and_threshold_gated():
    tr = regime.RegimeTracker(dict(CFG))
    assert tr.update(0.0, 1.0, 2.0)["spiking"] is True     # >= 1.5
    assert tr.update(0.0, 1.0, 1.0)["spiking"] is False
    assert tr.update(0.0, 1.0, None)["spiking"] is False   # no ROC yet


# ---------------------------------------------------------------------------
# GEX-cross grid
# ---------------------------------------------------------------------------

def test_grid_corners_and_diagonals():
    assert regime.combined_regime(regime.COMPRESSED, regime.POS_GEX) == \
        ("COMPRESSED/POS_GEX", regime.FADE)
    assert regime.combined_regime(regime.EXPANSIVE, regime.NEG_GEX) == \
        ("EXPANSIVE/NEG_GEX", regime.TREND)
    assert regime.combined_regime(regime.COMPRESSED, regime.NEG_GEX)[1] == regime.MIXED
    assert regime.combined_regime(regime.EXPANSIVE, regime.POS_GEX)[1] == regime.REDUCE
    assert regime.combined_regime(regime.NEUTRAL, regime.POS_GEX)[1] == regime.STAND_ASIDE


def test_gex_mapping_from_bias_dict():
    assert regime.map_gex_regime({"regime": "LONG_GAMMA"}) == regime.POS_GEX
    assert regime.map_gex_regime({"regime": "SHORT_GAMMA"}) == regime.NEG_GEX
    # Near-zero GEX carries no mechanism -> no grid column.
    assert regime.map_gex_regime({"regime": "NEUTRAL"}) == regime.UNKNOWN_GEX
    assert regime.map_gex_regime({"regime": "UNKNOWN"}) == regime.UNKNOWN_GEX
    assert regime.map_gex_regime(None) == regime.UNKNOWN_GEX


def test_tracker_passes_gex_into_combined():
    tr = regime.RegimeTracker(dict(CFG))
    for _ in range(3):
        out = tr.update(-1.5, 1.0, 0.0, gex_bias={"regime": "LONG_GAMMA"})
    assert out["combined_regime"] == "COMPRESSED/POS_GEX"
    assert out["grid_action"] == regime.FADE
