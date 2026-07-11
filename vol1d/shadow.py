# vol1d/shadow.py
# Shadow-mode evaluation: what the vol1d module WOULD do to each signal
# (spec §4), logged to vol1d_shadow for review. It gates NOTHING until
# vol1d.config enforce=True is flipped after that review.
#
# Verdicts:
#   ALLOW        geometry coherent, no filter tripped
#   DOWNWEIGHT   signal routed against the grid corner (fade setup in a
#                trend tape or vice versa) — reduce, don't refuse
#   VETO         target/stop geometry incoherent with exp_move_adj
#   STAND_ASIDE  spike in progress / opening grab window / no grid corner
#   ABSTAIN      module confidence too low to hold an opinion
#
# The module describes the SIZE of the field; GEX describes the mechanism;
# key levels describe the targets. This layer must stay an independent
# input — it sizes and filters, it never picks trades.

import json
from datetime import datetime, timezone

import db_utils
from vol1d import config as vol1d_config
from vol1d import regime as vol1d_regime

_DB = db_utils.data_path("vol1d_state.db")

ALLOW       = "ALLOW"
DOWNWEIGHT  = "DOWNWEIGHT"
VETO        = "VETO"
STAND_ASIDE = "STAND_ASIDE"
ABSTAIN     = "ABSTAIN"

# Which strategy families ride which grid corner (spec §4 routing).
_FADE_TYPES  = {"VWAP_MR", "VWAP_RECLAIM"}
_TREND_TYPES = {"ORB", "VWAP_TREND", "IB_EXT", "IB_EXTENSION",
                "ORB_PULLBACK", "OPENING_DRIVE"}


def _minutes_since_open(ts_et):
    return (ts_et.hour * 60 + ts_et.minute) - (9 * 60 + 30)


def size_mult(tod_z, cfg_gating):
    """Position-size multiplier: inverse to the detrended level, clamped.
    Warmup (z None) sizes flat at 1.0."""
    if tod_z is None:
        return 1.0
    raw = 1.0 - cfg_gating["sizing_slope"] * tod_z
    return round(max(cfg_gating["size_min"],
                     min(cfg_gating["size_max"], raw)), 3)


def evaluate_signal(sig, state, cfg=None, has_open_position=False,
                    now_et=None):
    """What vol1d would do to one scanner signal dict.

    Returns {"verdict", "reasons": [...], "size_mult"} — pure, no I/O.
    `state` is a Vol1DState (or None -> ABSTAIN).
    """
    cfg = cfg or vol1d_config.get_config()
    g = cfg["gating"]

    if state is None:
        return {"verdict": ABSTAIN, "reasons": ["no_state"], "size_mult": 1.0}

    reasons = []
    mult = size_mult(state.vix1d_tod_z, g)

    if state.confidence < g["min_confidence"]:
        return {"verdict": ABSTAIN,
                "reasons": ["confidence_{:.2f}".format(state.confidence)],
                "size_mult": mult}

    now_et = now_et or state.ts
    if now_et is not None and 0 <= _minutes_since_open(now_et) < g["open_grab_min"]:
        return {"verdict": STAND_ASIDE, "reasons": ["opening_grab_window"],
                "size_mult": mult}

    # Spike filter: don't chase the vol gap into a fresh position.
    if state.spiking and not has_open_position:
        return {"verdict": STAND_ASIDE, "reasons": ["vix1d_spiking"],
                "size_mult": mult}

    # --- Expected-move guardrail -----------------------------------------
    # exp_move_adj is in SPX points; signals are on the ETF proxies, so
    # compare in PERCENT of the underlying.
    price = sig.get("price")
    direction = sig.get("direction")
    stop = sig.get("und_call_stop") if direction == "CALL" else sig.get("und_put_stop")
    t1   = sig.get("und_call_t1")   if direction == "CALL" else sig.get("und_put_t1")
    if (price and state.exp_move_adj and state.spot
            and state.spot > 0 and price > 0):
        exp_pct = state.exp_move_adj / state.spot
        if t1:
            target_frac = abs(t1 - price) / (price * exp_pct)
            # In a FADE (dampened, compressed) tape a target beyond ~1 sigma
            # of the ADJUSTED expected move is incoherent.
            if (state.grid_action == vol1d_regime.FADE
                    and target_frac > g["exp_move_target_cap"]):
                reasons.append("target_{:.2f}sigma_beyond_exp_move".format(
                    target_frac))
        if stop:
            stop_frac = abs(price - stop) / (price * exp_pct)
            if stop_frac < g["stop_noise_floor"]:
                reasons.append("stop_{:.2f}sigma_inside_noise".format(stop_frac))
    if reasons:
        return {"verdict": VETO, "reasons": reasons, "size_mult": mult}

    # --- Fade-vs-trend routing against the grid corner --------------------
    stype = str(sig.get("signal_type") or "").upper()
    if state.grid_action == vol1d_regime.FADE and stype in _TREND_TYPES:
        return {"verdict": DOWNWEIGHT, "reasons": ["trend_setup_in_fade_tape"],
                "size_mult": mult}
    if state.grid_action == vol1d_regime.TREND and stype in _FADE_TYPES:
        return {"verdict": DOWNWEIGHT, "reasons": ["fade_setup_in_trend_tape"],
                "size_mult": mult}
    if state.grid_action in (vol1d_regime.MIXED, vol1d_regime.REDUCE):
        return {"verdict": DOWNWEIGHT,
                "reasons": ["grid_{}".format(state.grid_action.lower())],
                "size_mult": mult}
    if state.grid_action == vol1d_regime.STAND_ASIDE:
        # No corner (neutral vol or unknown GEX): nothing to say beyond size.
        return {"verdict": ALLOW, "reasons": ["no_grid_corner"],
                "size_mult": mult}

    return {"verdict": ALLOW, "reasons": [], "size_mult": mult}


def _init_shadow_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vol1d_shadow (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ts               TEXT,
            symbol           TEXT,
            direction        TEXT,
            signal_type      TEXT,
            grade            TEXT,
            verdict          TEXT,
            reasons          TEXT,
            size_mult        REAL,
            enforced         INTEGER,
            vix1d            REAL,
            vix1d_tod_z      REAL,
            vol_state        TEXT,
            combined_regime  TEXT,
            spiking          INTEGER,
            confidence       REAL
        )
    """)


def log_shadow(sig, state, verdict, enforced=False, db_path=None):
    """Persist one shadow decision. This IS the deliverable of shadow mode:
    the record reviewed before enforce is ever flipped."""
    conn = db_utils.connect(db_path or _DB)
    _init_shadow_table(conn)
    conn.execute("""
        INSERT INTO vol1d_shadow
        (ts, symbol, direction, signal_type, grade, verdict, reasons,
         size_mult, enforced, vix1d, vix1d_tod_z, vol_state,
         combined_regime, spiking, confidence)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        sig.get("symbol"), sig.get("direction"), sig.get("signal_type"),
        sig.get("grade"), verdict["verdict"],
        json.dumps(verdict.get("reasons") or []),
        verdict.get("size_mult"), 1 if enforced else 0,
        getattr(state, "vix1d", None),
        getattr(state, "vix1d_tod_z", None),
        getattr(state, "vol_state", None),
        getattr(state, "combined_regime", None),
        1 if getattr(state, "spiking", False) else 0,
        getattr(state, "confidence", None),
    ))
    conn.commit()
    conn.close()


def process_signals(signals, state, cfg=None, has_open_position=False,
                    db_path=None):
    """Evaluate + shadow-log every signal. Returns the list of signals that
    survive gating — which is ALL of them until enforce=True (the decisions
    are only logged). Never raises into the scan loop."""
    cfg = cfg or vol1d_config.get_config()
    enforce = bool(cfg.get("enforce"))
    allowed = []
    for sig in signals:
        try:
            verdict = evaluate_signal(sig, state, cfg,
                                      has_open_position=has_open_position)
            blocked = enforce and verdict["verdict"] in (VETO, STAND_ASIDE)
            log_shadow(sig, state, verdict, enforced=blocked, db_path=db_path)
            if blocked:
                continue
        except Exception as e:
            print("[vol1d.shadow] evaluation failed: {}".format(e))
        allowed.append(sig)
    return allowed
