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
from vol1d import gex_intraday as vol1d_gex_intraday
from vol1d import gex_live as vol1d_gex_live
from vol1d import proxy as vol1d_proxy
from vol1d import qa as vol1d_qa
from vol1d import regime as vol1d_regime
from vol1d.chain_source import CboeDelayedChainSource


def _default_spy_price():
    """Live SPY from the engine's Alpaca snapshot path; None when the
    freshest read is too old to call live. Lazy import keeps vol1d
    importable in offline tests."""
    try:
        import data_fetcher
        price, age, _src = data_fetcher.get_price_with_freshness("SPY")
    except Exception:
        return None
    max_age = vol1d_config.get_config()["gex_intraday"]["spy_max_age_secs"]
    if price is None or age is None or age > max_age:
        return None
    return price

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

    def __init__(self, chain_source=None, cfg=None, db_path=None,
                 spy_price_fn=None):
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
        # v2 intraday GEX (gex_intraday spec): slow-loop positioning curve
        # + fast-loop live-spot evaluation. Owns the session accumulators;
        # the updater being recreated each session resets the flow layer.
        gi = self.cfg["gex_intraday"]
        self.spy_price_fn = spy_price_fn or _default_spy_price
        self.gex_intraday = None                     # latest output contract
        self._gi_engine = None
        self._gi_built_at = None
        if gi["enabled"]:
            self._gi_engine = vol1d_gex_intraday.DelayedGEX(
                cfg=self.cfg, db_path=db_path)
            self._gi_proxy = vol1d_gex_intraday.SpotProxy(
                alpha=gi["basis_ema_alpha"],
                pair_tolerance_secs=gi["basis_pair_tolerance_secs"])
            self._gi_machine = vol1d_gex_intraday.RegimeMachine(gi)

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

        try:
            self._gex_intraday_pass(snap, now_et)
        except Exception as e:
            print("[vol1d] gex_intraday pass failed: {}".format(e))

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

    def _gex_intraday_pass(self, snap, now_et):
        """gex_intraday's two loops, riding the updater cadence.

        SLOW (profile_interval_secs throttle): ingest the chain into the
        positioning book (OI baseline + volume-diff flow), pair the delayed
        SPX spot with the buffered SPY sample at chain_ts (basis), rebuild
        the reusable curve, feed the regime machine, persist the snapshot.

        FAST (every pass): live SPY -> spot_est via the ratio basis, demote
        the machine on live flip crossings, and publish the evaluated
        output contract on self.gex_intraday."""
        if self._gi_engine is None:
            return
        gi = self.cfg["gex_intraday"]
        chain_ts = snap.get("chain_ts") or snap.get("ts") or now_et

        spy = None
        try:
            spy = self.spy_price_fn()
        except Exception:
            pass
        if spy:
            self._gi_proxy.record_live(now_et, spy)

        due = (self._gi_built_at is None
               or (now_et - self._gi_built_at).total_seconds()
               >= gi["profile_interval_secs"])
        stale = (now_et - chain_ts).total_seconds() > gi["stale_secs"]
        if due:
            self._gi_engine.on_snapshot(snap, now_et=now_et)
            self._gi_proxy.update_basis(snap.get("spot"), chain_ts)
            profile = self._gi_engine.build_profile(snap.get("spot"),
                                                    now_et=now_et)
            if profile is not None:
                self._gi_built_at = now_et
                spot_slow = self._gi_proxy.spot(spy) or snap.get("spot")
                try:
                    low = self._gi_engine.low_gex_threshold()
                except Exception:
                    low = None
                self._gi_machine.on_profile(profile, spot_slow,
                                            low_gex_threshold=low,
                                            stale=stale)
                try:
                    self._gi_engine.persist_snapshot(now_et=now_et)
                except Exception as e:
                    print("[vol1d] gex_intraday persist failed: {}".format(e))

        profile = self._gi_engine.profile
        if profile is None:
            return
        spot_est = self._gi_proxy.spot(spy)
        self._gi_machine.on_fast(profile, spot_est or snap.get("spot"),
                                 stale=stale)
        self.gex_intraday = vol1d_gex_intraday.evaluate(
            profile, spot_est, self._gi_machine, now_et=now_et,
            chain_ts=self._gi_engine.chain_ts, cfg_gi=gi)

    def _grid_gex(self, snap, now_et, snapshot_bias):
        """(gex_dict, source) the grid crosses against, in preference
        order: the v2 gex_intraday read (damped regime machine; its
        `transitional` deliberately maps to UNKNOWN_GEX -> stand aside),
        else the v1 gex_live recompute, else the nightly snapshot.

        The live compute is throttled (min_interval_secs) — dealer gamma
        migrates on minutes, not seconds — and discarded past max_age_secs
        so a stalled chain can't pin an old mechanism read."""
        gi_out = self.gex_intraday
        if gi_out is not None and not gi_out.get("stale"):
            return vol1d_gex_intraday.to_grid_bias(gi_out), "intraday"
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
