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

    "gex_live": {
        # Intraday dealer-GEX recomputed from the same delayed CBOE chain
        # the proxy reads. This re-prices YESTERDAY'S book (OI is a daily
        # OCC print) at live spot/IV — it captures flip-point crossings and
        # intraday gamma migration, not positions opened today.
        "enabled": True,
        # SPX (AM-settled monthlies) + SPXW (daily PM) — the full index
        # book, unlike the proxy strips which are SPXW-only.
        "roots": ["SPXW", "SPX"],
        # Only expiries within this many calendar days contribute (front
        # book dominates dealer hedging; matches the nightly build's
        # expiries_ahead spirit).
        "max_days_ahead": 7,
        # Refuse a read from a suspiciously thin chain.
        "min_contracts": 100,
        # Recompute throttle: GEX moves slower than the proxy level and
        # sums the whole book, so don't pay for it every 15s pass.
        "min_interval_secs": 60,
        # Beyond this age the live read is discarded and the grid falls
        # back to the nightly snapshot.
        "max_age_secs": 900,
        # |GEX $B| below this maps to no grid column (UNKNOWN). CALIBRATE:
        # index-chain dollar gamma runs far larger than the SPY-chain
        # numbers the nightly bias buckets with +/-1B.
        "neutral_band_b": 2.0,
    },

    "gex_intraday": {
        # v2 intraday GEX (delayed-feed spec): slow-loop positioning
        # profile from the delayed chain (OI baseline + unsigned volume-diff
        # flow), evaluated continuously at a live spot proxy. When enabled
        # and producing output it takes over the grid's GEX column from
        # gex_live (which stays as the fallback).
        "enabled": True,
        "roots": ["SPXW", "SPX"],
        # Expiries within this many calendar days feed net_gex_all (0DTE is
        # also published separately).
        "max_dte": 5,
        # Unsigned volume-diff flow weight vs the OI baseline (spec: 0.5;
        # validation step 3 decides whether the layer stays at all).
        "flow_weight": 0.5,
        # Slow-loop cadence: profile rebuild + snapshot persist throttle.
        # The chain fetch itself rides the existing ~15s updater pass.
        "profile_interval_secs": 300,
        # Chain age beyond which the read is flagged stale and the regime
        # machine freezes (no new flips).
        "stale_secs": 1200,
        # Regime machine: |spot - flip| band (percent of spot) inside which
        # the state is transitional, and how many consecutive slow-loop
        # profiles must agree on sign before positive/negative is emitted.
        "flip_band_pct": 0.15,
        "confirm_profiles": 2,
        # IV wing clamp for the per-strike mid IV.
        "iv_lo": 0.03,
        "iv_hi": 3.0,
        # Floor on T (minutes) so 0DTE gamma stays finite into settlement.
        "min_t_minutes": 15.0,
        # Refuse to publish a profile built from fewer strikes than this.
        "min_strikes": 20,
        # Spot proxy (SPY route): ratio = spx_delayed / (spy_at_chain_ts *
        # 10), EMA'd; live sample paired to chain_ts within tolerance.
        "basis_ema_alpha": 0.3,
        "basis_pair_tolerance_secs": 420,
        # Live SPY price older than this is not a live spot (fast loop
        # falls back to the delayed chain spot).
        "spy_max_age_secs": 90,
        # |net_gex_all| below its trailing p25 reads transitional. Skipped
        # (None) until min_sessions of history are banked.
        "pctile_lookback_sessions": 20,
        "pctile_min_sessions": 5,
        "pctile": 25,
        # Persist per-slow-tick compact snapshots (the free historical
        # dataset) and prune beyond this many days.
        "persist_snapshots": True,
        "persist_days": 30,
    },

    "gating": {
        # What the module WOULD do to a signal (spec §4). Shadow-only until
        # `enforce` above is flipped; every knob here is CALIBRATE-against-
        # shadow-logs material.
        #
        # Expected-move guardrail: veto when the target sits beyond this
        # many exp_move_adj sigmas in a FADE (COMPRESSED/POS_GEX) tape...
        "exp_move_target_cap": 1.0,
        # ...or when the stop is tighter than this fraction of expected
        # noise (it will be hit by wiggle, not by being wrong).
        "stop_noise_floor": 0.25,
        # Judas/liquidity-grab window: minutes after the open during which
        # the level is not actionable.
        "open_grab_min": 15,
        # Sizing: size_mult = 1 - sizing_slope * vix1d_tod_z, clamped.
        # Bigger when quiet/compressed, smaller when expansive.
        "sizing_slope": 0.25,
        "size_min": 0.5,
        "size_max": 1.25,
        # Below this confidence the module abstains (no veto authority).
        "min_confidence": 0.5,
    },

    "confidence": {
        # Multiplicative downgrades on Vol1DState.confidence (base 1.0).
        "baseline_warmup_mult": 0.6,   # tod_z from < min_sessions of history
        "no_baseline_mult":     0.3,   # no baseline at all (day-1 warmup)
        "residual_breach_mult": 0.5,   # proxy-vs-official residual over tolerance
        "no_gex_mult":          0.8,   # GEX column unknown -> grid unconfirmed
        "snapshot_gex_mult":    0.9,   # grid crossed a prior-close GEX (no live read)
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
        # Free, key-less delayed SPX/SPXW chain (includes bid/ask, OI, IV
        # and spot). ~15-min delayed — fine for the detrended regime read;
        # only the spike ROC sees the delay.
        "cboe_url": "https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json",
        "timeout_secs": 20,
        # Roots kept in the parsed snapshot. One fetch serves BOTH
        # consumers: the proxy filters down to proxy.roots (SPXW strips),
        # gex_live uses its own root set over the full index book.
        "roots": ["SPXW", "SPX"],
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
