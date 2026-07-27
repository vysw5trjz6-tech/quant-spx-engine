# Intraday GEX v2 (vol1d/gex_intraday): volume-diff flow layer, reusable
# profile with flip/walls, SPY-route spot proxy, damped regime state
# machine, output contract, snapshot persistence, and the updater's
# grid preference for the intraday read. No network.

import os
import tempfile
from datetime import date, datetime, timedelta

from gamma_exposure import net_gex_at_spot
from vol1d import config as vol1d_config
from vol1d import gex_intraday, regime
from vol1d import state as vol1d_state
from vol1d.chain_source import CboeDelayedChainSource, ChainSource


def _next_bday(d):
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


TODAY = _next_bday(date.today() + timedelta(days=1))
NEXT  = _next_bday(TODAY + timedelta(days=1))
NOW   = datetime(TODAY.year, TODAY.month, TODAY.day, 10, 0)
SPOT  = 6300.0


def _cfg(**over):
    cfg = vol1d_config.get_config()
    cfg["gex_intraday"].update({"min_strikes": 2, "profile_interval_secs": 300})
    cfg["gex_intraday"].update(over)
    return cfg


def _q(expiry, typ, strike, oi=0, vol=0, iv=0.2, bid=1.0, ask=1.2,
       root="SPXW"):
    return {"root": root, "expiry": expiry, "type": typ, "strike": strike,
            "bid": bid, "ask": ask, "open_interest": oi, "volume": vol,
            "iv": iv}


def _snap(quotes, ts=NOW, chain_ts=None, spot=SPOT):
    return {"ts": ts, "chain_ts": chain_ts or ts, "chain_ts_source": "payload",
            "spot": spot, "quotes": quotes, "source": "test"}


def _db():
    return os.path.join(tempfile.mkdtemp(prefix="gexi-"), "s.db")


# ---------------------------------------------------------------------------
# chain_source: volume + chain timestamp ride along
# ---------------------------------------------------------------------------

def test_payload_parse_carries_volume_and_chain_ts():
    payload = {
        "timestamp": "2026-07-20 10:05:00",
        "data": {
            "current_price": SPOT,
            "options": [
                {"option": "SPXW{:%y%m%d}C06300000".format(TODAY),
                 "bid": 5.0, "ask": 5.5, "open_interest": 1200,
                 "iv": 0.18, "volume": 431},
                {"option": "SPXW{:%y%m%d}P06300000".format(TODAY),
                 "bid": 4.0, "ask": 4.5},   # no volume in payload
            ],
        }}
    snap = CboeDelayedChainSource(roots=["SPXW"]).parse_payload(payload)
    by_type = {q["type"]: q for q in snap["quotes"]}
    assert by_type["call"]["volume"] == 431
    assert by_type["put"]["volume"] == 0            # degrades, not dropped
    assert snap["chain_ts"] == datetime(2026, 7, 20, 10, 5)
    assert snap["chain_ts_source"] == "payload"


def test_payload_without_timestamp_falls_back_to_fetch_time():
    payload = {"data": {"current_price": SPOT, "options": [
        {"option": "SPXW{:%y%m%d}C06300000".format(TODAY),
         "bid": 5.0, "ask": 5.5}]}}
    snap = CboeDelayedChainSource(roots=["SPXW"]).parse_payload(payload)
    assert snap["chain_ts_source"] == "fetch"
    assert snap["chain_ts"] == snap["ts"]


# ---------------------------------------------------------------------------
# Flow layer: cumulative volume diffing
# ---------------------------------------------------------------------------

def _engine(**over):
    over.setdefault("min_strikes", 1)
    return gex_intraday.DelayedGEX(cfg=_cfg(**over), db_path=_db())


def test_dvol_accumulates_and_is_cadence_invariant():
    coarse = _engine()
    coarse.on_snapshot(_snap([_q(TODAY, "call", 6300, oi=100, vol=0)]),
                       now_et=NOW)
    coarse.on_snapshot(_snap([_q(TODAY, "call", 6300, oi=100, vol=900)]),
                       now_et=NOW + timedelta(minutes=10))

    fine = _engine()
    for i, v in enumerate((0, 300, 600, 900)):
        fine.on_snapshot(_snap([_q(TODAY, "call", 6300, oi=100, vol=v)]),
                         now_et=NOW + timedelta(minutes=2 * i))

    key = (TODAY, 6300)
    assert coarse.rows[key].cum_dvol_call == 900
    assert fine.rows[key].cum_dvol_call == 900


def test_volume_decrease_is_clamped_not_negative():
    e = _engine()
    e.on_snapshot(_snap([_q(TODAY, "call", 6300, oi=100, vol=500)]), now_et=NOW)
    e.on_snapshot(_snap([_q(TODAY, "call", 6300, oi=100, vol=400)]),
                  now_et=NOW + timedelta(minutes=5))
    assert e.rows[(TODAY, 6300)].cum_dvol_call == 500


def test_session_rollover_resets_flow_accumulators():
    e = _engine()
    e.on_snapshot(_snap([_q(TODAY, "call", 6300, oi=100, vol=500)]), now_et=NOW)
    next_session = datetime(NEXT.year, NEXT.month, NEXT.day, 9, 40)
    e.on_snapshot(_snap([_q(NEXT, "call", 6300, oi=150, vol=50)]),
                  now_et=next_session)
    r = e.rows[(NEXT, 6300)]
    assert r.cum_dvol_call == 50 and r.oi_call == 150
    assert (TODAY, 6300) not in e.rows


def test_flow_layer_moves_positioning():
    base = _engine()
    base.on_snapshot(_snap([_q(TODAY, "call", 6300, oi=1000, vol=0)]),
                     now_et=NOW)
    p0 = base.build_profile(SPOT, now_et=NOW)

    flow = _engine()
    flow.on_snapshot(_snap([_q(TODAY, "call", 6300, oi=1000, vol=0)]),
                     now_et=NOW)
    flow.on_snapshot(_snap([_q(TODAY, "call", 6300, oi=1000, vol=800)]),
                     now_et=NOW + timedelta(minutes=5))
    p1 = flow.build_profile(SPOT, now_et=NOW + timedelta(minutes=5))

    # pos = oi + 0.5 * dvol -> 1400 vs 1000: flow half-weighted, additive.
    assert p1["net_gex_all"] > p0["net_gex_all"]


# ---------------------------------------------------------------------------
# Profile: signs, flip, walls, 0DTE split, reusable curve
# ---------------------------------------------------------------------------

def _two_sided_book():
    # Puts concentrated below spot, calls above: classic flip topology.
    quotes = []
    for k in (6200, 6220, 6240):
        quotes.append(_q(TODAY, "put", float(k), oi=5000))
    for k in (6350, 6370, 6390):
        quotes.append(_q(TODAY, "call", float(k), oi=5000))
    quotes.append(_q(NEXT, "call", 6350.0, oi=2000))
    return quotes


def test_profile_levels_and_0dte_split():
    e = _engine()
    e.on_snapshot(_snap(_two_sided_book()), now_et=NOW)
    p = e.build_profile(SPOT, now_et=NOW)
    assert p is not None
    # Dealer convention: calls +, puts -.
    assert p["gex_by_strike"][6350.0] > 0
    assert p["gex_by_strike"][6200.0] < 0
    # Flip sits between the put cluster and the call cluster.
    assert 6240 < p["flip"] < 6350
    assert p["call_wall"] == 6350.0          # 0DTE + NEXT stack there
    assert p["put_wall"] in (6200.0, 6220.0, 6240.0)
    # NEXT-expiry gamma is in ALL but not in 0DTE.
    assert abs(p["net_gex_0dte"]) < abs(p["net_gex_all"]) or \
        p["net_gex_all"] != p["net_gex_0dte"]


def test_thin_book_refused():
    e = gex_intraday.DelayedGEX(cfg=_cfg(min_strikes=50), db_path=_db())
    e.on_snapshot(_snap(_two_sided_book()), now_et=NOW)
    assert e.build_profile(SPOT, now_et=NOW) is None


def test_iv_clamp_and_gap_interpolation():
    e = _engine()
    quotes = [
        _q(TODAY, "call", 6280, oi=100, iv=0.20),
        _q(TODAY, "call", 6300, oi=100, iv=None, bid=0.0, ask=0.0),  # gap
        _q(TODAY, "call", 6320, oi=100, iv=0.30),
        _q(TODAY, "call", 6340, oi=100, iv=50.0),   # percent-form -> 0.5
        _q(TODAY, "call", 6360, oi=100, iv=9.0),    # 900%? -> /100 then clamp floor
    ]
    e.on_snapshot(_snap(quotes), now_et=NOW)
    p = e.build_profile(SPOT, now_et=NOW)
    # The gap strike still contributes (interpolated IV midway 0.20-0.30).
    assert p["gex_by_strike"][6300.0] > 0
    curve_iv = {k: iv for k, t, iv, pos, dte in p["curve"]}
    assert abs(curve_iv[6300.0] - 0.25) < 1e-9
    assert curve_iv[6340.0] == 0.5
    cfg = _cfg()["gex_intraday"]
    assert cfg["iv_lo"] <= curve_iv[6360.0] <= cfg["iv_hi"]


def test_flip_interpolation_zero_cross():
    # _flip_strike is now the LEGACY cumulative-by-strike diagnostic; it
    # still rides along on the profile as flip_cumulative, so its own
    # behaviour stays pinned.
    by_strike = {100.0: -10.0, 110.0: 30.0}
    # cum: -10 then +20 -> cross 1/3 into the gap
    flip = gex_intraday._flip_strike(by_strike)
    assert abs(flip - (100 + 10 / 3.0)) < 1e-6
    assert gex_intraday._flip_strike({100.0: 5.0, 110.0: 6.0}) is None


def test_profile_flip_is_a_root_of_net_gex():
    """The profile's headline flip is the spot where net gamma is zero,
    not the cumulative-by-strike crossing."""
    e = _engine()
    e.on_snapshot(_snap(_two_sided_book()), now_et=NOW)
    p = e.build_profile(SPOT, now_et=NOW)

    # Net gamma changes sign within a cent of the reported level. Stated
    # as a price bracket rather than a residual tolerance because this is
    # a 0DTE book: gamma is steep enough that a sub-cent error in spot
    # still moves net GEX by six figures.
    flip = p["flip"]
    assert net_gex_at_spot(p["curve"], flip - 0.01) < 0
    assert net_gex_at_spot(p["curve"], flip + 0.01) > 0
    # Short gamma below it, long gamma above it.
    assert net_gex_at_spot(p["curve"], flip * 0.995) < 0
    assert net_gex_at_spot(p["curve"], flip * 1.005) > 0


def test_profile_carries_legacy_flip_for_audit():
    e = _engine()
    e.on_snapshot(_snap(_two_sided_book()), now_et=NOW)
    p = e.build_profile(SPOT, now_et=NOW)
    assert p["flip_cumulative"] == gex_intraday._flip_strike(p["gex_by_strike"])
    # The two are distinct measures; the legacy one is not a root.
    assert p["flip"] != p["flip_cumulative"]


def test_snapshot_persists_both_flip_measures():
    db = _db()
    e = gex_intraday.DelayedGEX(cfg=_cfg(), db_path=db)
    e.on_snapshot(_snap(_two_sided_book()), now_et=NOW)
    p = e.build_profile(SPOT, now_et=NOW)
    e.persist_snapshot(now_et=NOW)

    conn = gex_intraday._connect(db)
    row = conn.execute(
        "SELECT flip, flip_cumulative FROM gex_intraday_snapshots").fetchone()
    conn.close()
    assert row[0] == p["flip"]
    assert row[1] == p["flip_cumulative"]


# ---------------------------------------------------------------------------
# Spot proxy (SPY route)
# ---------------------------------------------------------------------------

def test_spot_proxy_pairs_at_chain_ts_and_estimates():
    px = gex_intraday.SpotProxy(alpha=1.0, pair_tolerance_secs=120)
    # Live SPY samples through time; SPX/SPY*10 basis is 1.002.
    for m in range(0, 20, 1):
        px.record_live(NOW + timedelta(minutes=m), 628.0 + 0.1 * m)
    # Delayed chain: ts NOW+15m carries SPX as of NOW (15-min lag).
    spy_then = 628.0
    spx_then = spy_then * 10 * 1.002
    px.update_basis(spx_then, chain_ts=NOW)
    est = px.spot(spy_live=630.0)
    assert abs(est - 630.0 * 10 * 1.002) < 1e-6


def test_spot_proxy_refuses_pairing_outside_tolerance():
    px = gex_intraday.SpotProxy(pair_tolerance_secs=60)
    px.record_live(NOW, 628.0)
    px.update_basis(6300.0, chain_ts=NOW + timedelta(minutes=30))
    assert px.ratio_ema is None
    assert px.spot(630.0) is None


# ---------------------------------------------------------------------------
# Regime state machine: damping
# ---------------------------------------------------------------------------

def _profile(flip=6250.0, net=5e9):
    return {"flip": flip, "net_gex_all": net, "spot_ref": SPOT,
            "call_wall": 6350.0, "put_wall": 6200.0, "curve": [],
            "net_gex_0dte": net / 2, "chain_ts": NOW, "built_at": NOW,
            "gex_by_strike": {}}


def test_single_profile_stays_transitional():
    m = gex_intraday.RegimeMachine(_cfg()["gex_intraday"])
    assert m.on_profile(_profile(), SPOT) == "transitional"


def test_two_agreeing_profiles_promote():
    m = gex_intraday.RegimeMachine(_cfg()["gex_intraday"])
    m.on_profile(_profile(), SPOT)
    assert m.on_profile(_profile(), SPOT) == "positive"
    # Below the flip on both -> negative.
    m2 = gex_intraday.RegimeMachine(_cfg()["gex_intraday"])
    m2.on_profile(_profile(flip=6350.0), SPOT)
    assert m2.on_profile(_profile(flip=6350.0), SPOT) == "negative"


def test_flip_band_forces_transitional():
    m = gex_intraday.RegimeMachine(_cfg()["gex_intraday"])
    near = _profile(flip=SPOT * (1 - 0.0005))     # 0.05% away < 0.15% band
    m.on_profile(near, SPOT)
    assert m.on_profile(near, SPOT) == "transitional"


def test_fast_loop_demotes_but_never_promotes():
    m = gex_intraday.RegimeMachine(_cfg()["gex_intraday"])
    m.on_profile(_profile(), SPOT)
    m.on_profile(_profile(), SPOT)
    assert m.state == "positive"
    # Spot crosses below the flip intraday -> demote NOW.
    assert m.on_fast(_profile(), 6200.0) == "transitional"
    # Fast loop alone can't bring it back.
    assert m.on_fast(_profile(), SPOT) == "transitional"
    # Slow-loop confirmation can.
    assert m.on_profile(_profile(), SPOT) == "positive"


def test_stale_freezes_machine():
    m = gex_intraday.RegimeMachine(_cfg()["gex_intraday"])
    m.on_profile(_profile(), SPOT)
    m.on_profile(_profile(), SPOT)
    assert m.state == "positive"
    assert m.on_fast(_profile(), 6200.0, stale=True) == "positive"
    assert m.on_profile(_profile(flip=6350.0), SPOT, stale=True) == "positive"


def test_low_net_gex_reads_transitional():
    m = gex_intraday.RegimeMachine(_cfg()["gex_intraday"])
    weak = _profile(net=1e8)
    m.on_profile(weak, SPOT, low_gex_threshold=5e8)
    assert m.on_profile(weak, SPOT, low_gex_threshold=5e8) == "transitional"


# ---------------------------------------------------------------------------
# Output contract + grid mapping
# ---------------------------------------------------------------------------

def test_evaluate_contract_and_distances():
    e = _engine()
    e.on_snapshot(_snap(_two_sided_book()), now_et=NOW)
    p = e.build_profile(SPOT, now_et=NOW)
    m = gex_intraday.RegimeMachine(_cfg()["gex_intraday"])
    m.on_profile(p, SPOT)
    m.on_profile(p, SPOT)

    out = gex_intraday.evaluate(p, 6310.0, m, now_et=NOW,
                                chain_ts=NOW - timedelta(minutes=15),
                                cfg_gi=_cfg()["gex_intraday"])
    for key in ("ts", "chain_ts", "stale", "spot_est", "net_gex_all",
                "net_gex_0dte", "net_gex_live_est", "flip", "call_wall",
                "put_wall", "regime", "spot_vs_flip_pct",
                "dist_to_call_wall_pct", "dist_to_put_wall_pct"):
        assert key in out
    assert out["stale"] is False
    assert out["spot_est"] == 6310.0
    assert out["spot_source"] == "proxy"
    assert out["net_gex_live_est"] is not None      # re-evaluated at 6310
    assert out["dist_to_call_wall_pct"] > 0         # wall above spot
    assert out["dist_to_put_wall_pct"] > 0          # wall below spot


def test_evaluate_flags_stale_and_falls_back_to_chain_spot():
    e = _engine()
    e.on_snapshot(_snap(_two_sided_book()), now_et=NOW)
    p = e.build_profile(SPOT, now_et=NOW)
    out = gex_intraday.evaluate(p, None, None, now_et=NOW,
                                chain_ts=NOW - timedelta(minutes=25),
                                cfg_gi=_cfg()["gex_intraday"])
    assert out["stale"] is True
    assert out["spot_source"] == "chain_delayed"
    assert out["spot_est"] == SPOT
    assert out["regime"] == "transitional"


def test_to_grid_bias_vocabulary():
    assert gex_intraday.to_grid_bias({"regime": "positive"})["regime"] == \
        "LONG_GAMMA"
    assert gex_intraday.to_grid_bias({"regime": "negative"})["regime"] == \
        "SHORT_GAMMA"
    t = gex_intraday.to_grid_bias({"regime": "transitional"})
    assert regime.map_gex_regime(t) == regime.UNKNOWN_GEX


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_snapshot_persistence_roundtrip():
    db = _db()
    e = gex_intraday.DelayedGEX(cfg=_cfg(), db_path=db)
    e.on_snapshot(_snap(_two_sided_book()), now_et=NOW)
    e.build_profile(SPOT, now_et=NOW)
    e.persist_snapshot(now_et=NOW)

    conn = gex_intraday._connect(db)
    rows = conn.execute("SELECT session_date, net_gex_all, flip, rows_blob "
                        "FROM gex_intraday_snapshots").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == NOW.strftime("%Y-%m-%d")
    assert rows[0][1] == e.profile["net_gex_all"]
    import json
    import zlib
    compact = json.loads(zlib.decompress(rows[0][3]))
    assert any(r[1] == 6350.0 and r[2] == 5000 for r in compact)


def test_low_gex_threshold_needs_history():
    db = _db()
    e = gex_intraday.DelayedGEX(cfg=_cfg(), db_path=db)
    e.on_snapshot(_snap(_two_sided_book()), now_et=NOW)
    e.build_profile(SPOT, now_et=NOW)
    e.persist_snapshot(now_et=NOW)
    assert e.low_gex_threshold() is None       # 1 session < min_sessions


# ---------------------------------------------------------------------------
# Updater integration: intraday takes the grid, damped
# ---------------------------------------------------------------------------

class _BookSource(ChainSource):
    """Calls-heavy chain with OI + volume so the intraday layer engages;
    strikes wide enough for the VIX1D proxy to compute too."""

    def __init__(self, now=NOW):
        self.now = now

    def get_snapshot(self):
        import vol_math
        from vol1d import daycount
        quotes = []
        for expiry in (TODAY, NEXT):
            t = daycount.business_time_to_expiry(self.now, expiry)
            for k in range(int(SPOT * 0.96), int(SPOT * 1.04), 5):
                for typ, oi in (("call", 5000), ("put", 100)):
                    mid = vol_math.bs_price(SPOT, float(k), t, 0.0, 0.2, typ)
                    live = mid >= 0.05
                    quotes.append({
                        "root": "SPXW", "expiry": expiry, "type": typ,
                        "strike": float(k),
                        "bid": mid * 0.99 if live else 0.0,
                        "ask": mid * 1.01 if live else 0.05,
                        "open_interest": oi, "volume": 10, "iv": 0.2,
                    })
        return {"ts": self.now, "chain_ts": self.now,
                "chain_ts_source": "payload", "spot": SPOT,
                "quotes": quotes, "source": "test"}


def _upd_cfg():
    cfg = vol1d_config.get_config()
    cfg["proxy"]["risk_free_rate"] = 0.0
    cfg["gex_intraday"].update({"profile_interval_secs": 300})
    cfg["gex_live"].update({"min_contracts": 2, "neutral_band_b": 0.001})
    return cfg


def test_updater_intraday_takes_grid_after_confirmation(tmp_path):
    src = _BookSource()
    up = vol1d_state.Vol1DUpdater(chain_source=src, cfg=_upd_cfg(),
                                  db_path=str(tmp_path / "s.db"),
                                  spy_price_fn=lambda: SPOT / 10.0)
    # First slow tick: one profile -> transitional -> UNKNOWN_GEX.
    st = up.compute_once(now_et=NOW, gex_bias={"regime": "SHORT_GAMMA"})
    assert st.gex_source == "intraday"
    assert up.gex_intraday["regime"] == "transitional"
    assert st.combined_regime.endswith("UNKNOWN_GEX")

    # Second slow tick 5 min later: confirmation -> positive (calls-heavy,
    # all-positive curve) despite the SHORT_GAMMA nightly snapshot.
    src.now = NOW + timedelta(minutes=5)
    st = up.compute_once(now_et=src.now, gex_bias={"regime": "SHORT_GAMMA"})
    assert up.gex_intraday["regime"] == "positive"
    assert st.gex_source == "intraday"
    assert st.combined_regime.endswith("POS_GEX")
    # Contract published with a proxy-estimated spot.
    assert up.gex_intraday["spot_source"] == "proxy"
    assert abs(up.gex_intraday["spot_est"] - SPOT) < SPOT * 0.001


def test_updater_intraday_disabled_falls_back(tmp_path):
    cfg = _upd_cfg()
    cfg["gex_intraday"]["enabled"] = False
    up = vol1d_state.Vol1DUpdater(chain_source=_BookSource(), cfg=cfg,
                                  db_path=str(tmp_path / "s.db"))
    st = up.compute_once(now_et=NOW, gex_bias=None)
    assert up.gex_intraday is None
    assert st.gex_source == "live"           # v1 gex_live path still works
