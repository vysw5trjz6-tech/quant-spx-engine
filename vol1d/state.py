# vol1d/state.py
# Vol1DState — the ONLY object the rest of the engine reads (spec §5) —
# and the updater that assembles it each pass.
#
# The module is a signal/filter layer, not an order router: it never takes
# a trade on its own, and until vol1d.config enforce=True it must not gate
# live orders either — it only logs what it would have done (shadow mode).

from dataclasses import dataclass, asdict
from datetime import datetime

import pytz

from vol1d import baseline as vol1d_baseline
from vol1d import config as vol1d_config
from vol1d import features as vol1d_features
from vol1d import gex_live as vol1d_gex_live
from vol1d import proxy as vol1d_proxy
from vol1d import qa as vol1d_qa
from vol1d import regime as vol1d_regime
from vol1d.chain_source import CboeDelayedChainSource

ET = pytz.timezone("America/New_York")


@dataclass(frozen=True)
class Vol1DState:
    ts: datetime
    vix1d: float
    exp_move_pts: float
    exp_move_adj: float
    vix1d_tod_z: float          # None during day-1 warmup
    iv_rv_spread: float         # None until RV window fills
    vix1d_roc: float            # None until ROC window fills
    vol_state: str              # COMPRESSED | NEUTRAL | EXPANSIVE
    spiking: bool
    combined_regime: str        # vol_state x GEX column
    confidence: float           # downgraded when data QA flags
    # Diagnostics beyond the spec's core fields:
    grid_action: str            # FADE | TREND | MIXED | REDUCE | STAND_ASIDE
    spot: float
    baseline_sessions: int
    qa_residual: float          # latest proxy-vs-official residual (or None)
    gex_source: str = "none"    # live | snapshot | none — what the grid crossed

    def to_dict(self):
        d = asdict(self)
        d["ts"] = self.ts.isoformat() if self.ts else None
        return d


def compute_confidence(tod_z, n_sessions, residual, gex_label, iv_rv_spread,
                       cfg, gex_source="live"):
    """Multiplicative downgrade stack, floor 0."""
    c = cfg["confidence"]
    conf = 1.0
    if tod_z is None:
        conf *= c["no_baseline_mult"]
    elif n_sessions < cfg["tod_baseline"]["min_sessions"]:
        conf *= c["baseline_warmup_mult"]
    if not vol1d_qa.residual_within_tolerance(residual, cfg):
        conf *= c["residual_breach_mult"]
    if gex_label == vol1d_regime.UNKNOWN_GEX:
        conf *= c["no_gex_mult"]
    elif gex_source == "snapshot":
        # The grid crossed a prior-close GEX read — mechanism may have
        # migrated intraday without us seeing it.
        conf *= c["snapshot_gex_mult"]
    if iv_rv_spread is None:
        conf *= c["no_rv_mult"]
    return round(conf, 3)


class Vol1DUpdater(object):
    """Owns the chain source and the session-scoped accumulators. main.py
    runs one instance on a dedicated ~update_secs thread and publishes the
    returned Vol1DState into _market_state under its lock."""

    def __init__(self, chain_source=None, cfg=None, db_path=None):
        self.cfg = cfg or vol1d_config.get_config()
        self.source = chain_source or CboeDelayedChainSource()
        self.features = vol1d_features.IntradayFeatures(self.cfg)
        self.tracker = vol1d_regime.RegimeTracker(self.cfg["regime"])
        self.db_path = db_path                       # None -> default store
        self._holidays = frozenset(self.cfg["proxy"].get("holidays") or ())
        self._residual_cache = {"date": None, "value": None}
        # Latest intraday GEX read (published by main next to gex_bias).
        self.gex_live = None
        self._gex_live_at = None

    def _latest_residual(self, today_iso):
        """Yesterday's reconcile result, fetched once per session."""
        if self._residual_cache["date"] != today_iso:
            try:
                _, resid = vol1d_qa.latest_residual(db_path=self.db_path)
            except Exception:
                resid = None
            self._residual_cache.update({"date": today_iso, "value": resid})
        return self._residual_cache["value"]

    def compute_once(self, now_et=None, gex_bias=None):
        """One full pass: fetch chain -> proxy -> features -> detrend ->
        regime -> Vol1DState. Returns None when the chain or proxy is
        unavailable (callers keep the previous state)."""
        now_et = now_et or datetime.now(ET).replace(tzinfo=None)

        snap = self.source.get_snapshot()
        if snap is None:
            return None
        out = vol1d_proxy.compute_vix1d(snap, now_et=now_et, cfg=self.cfg,
                                        holidays=self._holidays)
        if out is None:
            return None
        level = out["vix1d"]
        spot  = snap.get("spot")

        # Bank the tick for the nightly baseline rebuild BEFORE reading the
        # baseline (today's ticks never contaminate today's z — the curve
        # only rebuilds nightly).
        try:
            vol1d_baseline.record_tick(now_et, level, db_path=self.db_path)
        except Exception as e:
            print("[vol1d] tick store failed: {}".format(e))

        feats = self.features.update(now_et, spot, level)
        tod_z, n_sessions = vol1d_baseline.tod_z(now_et, level, cfg=self.cfg,
                                                 db_path=self.db_path)

        grid_gex, gex_source = self._grid_gex(snap, now_et, gex_bias)
        reg = self.tracker.update(tod_z, feats["iv_rv_spread"],
                                  feats["vix1d_roc"], gex_bias=grid_gex)

        residual = self._latest_residual(now_et.strftime("%Y-%m-%d"))
        conf = compute_confidence(tod_z, n_sessions, residual,
                                  reg["gex_label"], feats["iv_rv_spread"],
                                  self.cfg, gex_source=gex_source)

        return Vol1DState(
            ts=now_et,
            vix1d=level,
            exp_move_pts=feats["exp_move_pts"],
            exp_move_adj=feats["exp_move_adj"],
            vix1d_tod_z=tod_z,
            iv_rv_spread=feats["iv_rv_spread"],
            vix1d_roc=feats["vix1d_roc"],
            vol_state=reg["vol_state"],
            spiking=reg["spiking"],
            combined_regime=reg["combined_regime"],
            confidence=conf,
            grid_action=reg["grid_action"],
            spot=spot,
            baseline_sessions=n_sessions,
            qa_residual=residual,
            gex_source=gex_source,
        )

    def _grid_gex(self, snap, now_et, snapshot_bias):
        """(gex_dict, source) the grid crosses against: the intraday read
        recomputed from this chain when fresh, else the nightly snapshot.

        The live compute is throttled (min_interval_secs) — dealer gamma
        migrates on minutes, not seconds — and discarded past max_age_secs
        so a stalled chain can't pin an old mechanism read."""
        g = self.cfg["gex_live"]
        if g["enabled"]:
            due = (self._gex_live_at is None
                   or (now_et - self._gex_live_at).total_seconds()
                   >= g["min_interval_secs"])
            if due:
                try:
                    live = vol1d_gex_live.compute_bias(snap, cfg=self.cfg,
                                                       today=now_et.date())
                except Exception as e:
                    print("[vol1d] gex_live compute failed: {}".format(e))
                    live = None
                if live is not None:
                    self.gex_live = live
                    self._gex_live_at = now_et
            fresh = (self.gex_live is not None
                     and self._gex_live_at is not None
                     and (now_et - self._gex_live_at).total_seconds()
                     <= g["max_age_secs"])
            if fresh:
                return self.gex_live, "live"
        if snapshot_bias:
            return snapshot_bias, "snapshot"
        return None, "none"


def session_close_level(session_date_iso, db_path=None):
    """Last banked proxy level of a session — the 'proxy close' the nightly
    QA reconcile compares against the official print."""
    conn = vol1d_baseline._connect(db_path)
    row = conn.execute(
        "SELECT level FROM vol1d_ticks WHERE session_date = ? "
        "ORDER BY minute_of_day DESC LIMIT 1", (session_date_iso,)).fetchone()
    conn.close()
    return row[0] if row else None


def run_nightly_jobs(session_date_iso=None, cfg=None, db_path=None,
                     official_source=None):
    """EOD job body (call after the close on trading days):
    1. reconcile today's proxy close vs the official VIX1D close,
    2. rebuild the minute-of-day baseline including today.
    Returns a small summary dict for logging."""
    cfg = cfg or vol1d_config.get_config()
    if session_date_iso is None:
        session_date_iso = datetime.now(ET).strftime("%Y-%m-%d")

    proxy_close = session_close_level(session_date_iso, db_path)
    qa_out = None
    if proxy_close is not None:
        qa_out = vol1d_qa.reconcile_daily(session_date_iso, proxy_close,
                                          source=official_source, cfg=cfg,
                                          db_path=db_path)
    minutes = vol1d_baseline.rebuild_baseline(cfg=cfg, db_path=db_path)
    return {
        "session":       session_date_iso,
        "proxy_close":   proxy_close,
        "qa":            qa_out,
        "baseline_minutes": minutes,
        "sessions_banked":  vol1d_baseline.sessions_banked(db_path),
    }
