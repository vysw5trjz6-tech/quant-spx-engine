# vol1d/config.py
# Every tunable for the vol1d module lives here — nothing hard-coded in the
# computation modules. Mirrors the YAML block in docs/vix1d_module_spec.md.
#
# Deliberately NOT part of main.DEFAULT_CONFIG: that dict is AI-tunable and
# persisted through the AI improvement loop, and the vol1d thresholds must
# only move by explicit human calibration. Operational knobs can be
# overridden per-deployment via environment variables (VOL1D_*) without a
# code change.

import copy
import os

DEFAULTS = {
    # Recompute cadence for the intraday updater thread (seconds). ~15s
    # matches the official index refresh; the CBOE delayed-quotes source is
    # free so cadence is not a cost decision.
    "update_secs": 15,

    # SHADOW MODE SWITCH. False = the module only logs its labels and what
    # it would have done; it must never gate live alerts/orders until this
    # is flipped after reviewing shadow logs.
    "enforce": False,

    "proxy": {
        # Business-day year (the VIX1D-specific deviation from standard
        # VIX): T = business_time_to_expiry / business_day_year.
        "business_day_year": 252,
        # SPXW is PM-settled at 4:00 PM ET.
        "settle_hour_et": 16.0,
        # OPRA roots making up the strips. SPXW carries every daily
        # (PM-settled) expiry; the SPX root is AM-settled monthlies and is
        # excluded from the two nearest daily strips by default.
        "roots": ["SPXW"],
        # Strike-selection stop rule: walking OTM outward from K0, stop
        # after this many CONSECUTIVE no-bid strikes (zero-bid strikes are
        # dropped either way).
        "consecutive_no_bid_stop": 2,
        # Flat short rate for e^(RT); at a 1-day horizon the sensitivity is
        # tiny (calibrate against the official print, not the curve).
        "risk_free_rate": 0.045,
        # Constant-maturity target horizon, in business days. w1/w2 fall
        # out of the standard Cboe near/next interpolation against this
        # target, so near-term weight rolls to 0 as T1 decays to the
        # horizon complement through the session.
        "target_horizon_bd": 1.0,
        # Drop the 0DTE strip entirely when it has fewer business minutes
        # than this left to settlement (quotes go degenerate at the death).
        "min_t1_minutes": 5.0,
        # Sanity bounds on the resulting level (annualized vol, %).
        "level_lo": 2.0,
        "level_hi": 200.0,
        # Market holidays (ISO dates) the business-time day count must
        # skip. Weekends are automatic; keep this list current or accept
        # a mis-scaled T across holiday gaps.
        "holidays": [],
    },

    "regime": {
        "low_z": -0.75,
        "high_z": 0.75,
        # vix1d_roc threshold for the spike overlay, in vol points over the
        # roc window. CALIBRATE against shadow logs before trusting.
        "roc_spike_thresh": 1.5,
        "roc_window_min": 10,
        # Consecutive updates a new vol_state must persist before the
        # emitted label flips (anti flip-flop).
        "hysteresis_bars": 3,
    },

    "tod_baseline": {
        "lookback_sessions": 60,
        "granularity": "minute",
        # Below this many banked sessions the tod_z is low-confidence and
        # the state's confidence field is downgraded.
        "min_sessions": 20,
        # Floor on the per-minute SD so a freakishly-quiet training window
        # can't turn ordinary wiggle into a huge |z|.
        "min_sd": 0.25,
    },

    "features": {
        # Risk-premium shrink: VIX1D overstates realized. CALIBRATE.
        "rp_factor": 0.85,
        # Rolling window (minutes) for intraday realized vol.
        "rv_window_min": 30,
    },

    "confidence": {
        # Multiplicative downgrades on Vol1DState.confidence (base 1.0).
        "baseline_warmup_mult": 0.6,   # tod_z from < min_sessions of history
        "no_baseline_mult":     0.3,   # no baseline at all (day-1 warmup)
        "residual_breach_mult": 0.5,   # proxy-vs-official residual over tolerance
        "no_gex_mult":          0.8,   # GEX column unknown -> grid unconfirmed
        "no_rv_mult":           0.9,   # intraday RV not yet computable
    },

    "qa": {
        "reconcile_with_official": True,
        # |proxy close - official close| beyond this (vol points) flags
        # data quality and downgrades confidence. CALIBRATE from the first
        # weeks of residual logs.
        "residual_tolerance": 2.0,
    },

    "chain_source": {
        # Free, key-less delayed SPX/SPXW chain (includes bid/ask + spot).
        # ~15-min delayed — fine for the detrended regime read; only the
        # spike ROC sees the delay.
        "cboe_url": "https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json",
        "timeout_secs": 20,
    },
}

# Environment overrides for the operational knobs only (thresholds move by
# code review, not env fiddling).
_ENV_OVERRIDES = (
    ("VOL1D_UPDATE_SECS", ("update_secs",), int),
    ("VOL1D_ENFORCE",     ("enforce",),     lambda v: v.strip().lower() in ("1", "true", "yes")),
)


def get_config():
    """Deep copy of the vol1d config with env overrides applied."""
    cfg = copy.deepcopy(DEFAULTS)
    for env_key, path, cast in _ENV_OVERRIDES:
        raw = os.getenv(env_key, "").strip()
        if not raw:
            continue
        try:
            val = cast(raw)
        except (ValueError, TypeError):
            continue
        node = cfg
        for k in path[:-1]:
            node = node[k]
        node[path[-1]] = val
    return cfg
