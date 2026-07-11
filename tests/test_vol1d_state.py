# End-to-end tests for vol1d/state.py with a stubbed chain source: the
# updater must assemble a full Vol1DState from a synthetic chain, bank
# ticks for the nightly rebuild, and downgrade confidence on warmup /
# missing GEX / QA breaches. No network.

import math
import os
import tempfile
from datetime import date, datetime, timedelta

import vol_math
from vol1d import baseline, qa, regime
from vol1d import config as vol1d_config
from vol1d import state as vol1d_state
from vol1d.chain_source import ChainSource
from vol1d import daycount


NOW   = datetime(2026, 7, 8, 10, 0)
TODAY = date(2026, 7, 8)
NEXT  = date(2026, 7, 9)
SPOT  = 6300.0


class StubSource(ChainSource):
    """Synthetic flat-vol chain at a controllable vol level."""

    def __init__(self, sigma=0.20, now=NOW):
        self.sigma = sigma
        self.now = now

    def get_snapshot(self):
        quotes = []
        for expiry in (TODAY, NEXT):
            t = daycount.business_time_to_expiry(self.now, expiry)
            for k in range(int(SPOT * 0.95), int(SPOT * 1.05), 5):
                for typ in ("call", "put"):
                    mid = vol_math.bs_price(SPOT, float(k), t, 0.0,
                                            self.sigma, typ)
                    if mid >= 0.05:
                        quotes.append({"root": "SPXW", "expiry": expiry,
                                       "type": typ, "strike": float(k),
                                       "bid": mid * 0.99, "ask": mid * 1.01})
                    else:
                        quotes.append({"root": "SPXW", "expiry": expiry,
                                       "type": typ, "strike": float(k),
                                       "bid": 0.0, "ask": 0.05})
        return {"ts": self.now, "spot": SPOT, "quotes": quotes,
                "source": "stub"}


def _cfg():
    cfg = vol1d_config.get_config()
    cfg["proxy"]["risk_free_rate"] = 0.0
    return cfg


def _db():
    return os.path.join(tempfile.mkdtemp(prefix="vol1d-state-"), "state.db")


def test_updater_assembles_state_and_banks_tick():
    db = _db()
    up = vol1d_state.Vol1DUpdater(chain_source=StubSource(), cfg=_cfg(),
                                  db_path=db)
    st = up.compute_once(now_et=NOW, gex_bias={"regime": "LONG_GAMMA"})
    assert st is not None
    assert abs(st.vix1d - 20.0) < 0.5
    assert st.exp_move_pts == round(SPOT * st.vix1d / 100 / math.sqrt(252), 2)
    assert st.exp_move_adj < st.exp_move_pts
    assert st.vol_state == regime.NEUTRAL        # warmup -> no opinion
    assert st.vix1d_tod_z is None                # no baseline yet
    assert st.combined_regime.endswith("POS_GEX")
    # The tick must be banked for tonight's baseline rebuild.
    assert vol1d_state.session_close_level(NOW.strftime("%Y-%m-%d"), db) is not None


def test_confidence_downgrades_stack():
    cfg = _cfg()
    c = cfg["confidence"]
    # Day-1 warmup, no GEX, no RV, no residual data.
    conf = vol1d_state.compute_confidence(
        None, 0, None, regime.UNKNOWN_GEX, None, cfg)
    assert conf == round(c["no_baseline_mult"] * c["no_gex_mult"]
                         * c["no_rv_mult"], 3)
    # Fully warmed, GEX known, RV live, residual within tolerance -> 1.0.
    assert vol1d_state.compute_confidence(
        0.5, 60, 0.5, regime.POS_GEX, 1.0, cfg) == 1.0
    # Residual breach halves it.
    assert vol1d_state.compute_confidence(
        0.5, 60, 99.0, regime.POS_GEX, 1.0, cfg) == c["residual_breach_mult"]
    # Partial warmup (some sessions but under min) uses the warmup mult.
    assert vol1d_state.compute_confidence(
        0.5, 5, 0.5, regime.POS_GEX, 1.0, cfg) == c["baseline_warmup_mult"]


def test_nightly_jobs_rebuild_and_reconcile():
    db = _db()
    up = vol1d_state.Vol1DUpdater(chain_source=StubSource(), cfg=_cfg(),
                                  db_path=db)
    # A few passes through the session bank ticks.
    for i in range(3):
        up.compute_once(now_et=NOW + timedelta(minutes=5 * i),
                        gex_bias=None)

    class _Official(qa.OfficialVix1dSource):
        def get_history(self):
            return {NOW.strftime("%Y-%m-%d"): 20.5}

    out = vol1d_state.run_nightly_jobs(NOW.strftime("%Y-%m-%d"), cfg=_cfg(),
                                       db_path=db, official_source=_Official())
    assert out["proxy_close"] is not None
    assert out["qa"]["official_close"] == 20.5
    assert abs(out["qa"]["residual"] - (out["proxy_close"] - 20.5)) < 1e-9
    assert out["baseline_minutes"] > 0
    assert out["sessions_banked"] == 1
    # After the rebuild, tod_z resolves on the next pass.
    z, n = baseline.tod_z(NOW, out["proxy_close"], db_path=db)
    assert z is not None and n == 1


def test_updater_returns_none_on_dead_source():
    class _Dead(ChainSource):
        def get_snapshot(self):
            return None

    up = vol1d_state.Vol1DUpdater(chain_source=_Dead(), cfg=_cfg(), db_path=_db())
    assert up.compute_once(now_et=NOW) is None
