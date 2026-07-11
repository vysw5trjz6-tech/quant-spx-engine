# Shadow-mode gating (spec §4 + §7): the module logs what it WOULD do and
# filters nothing until enforce=True. These tests pin every verdict path,
# the sizing curve, and — critically — that shadow mode never drops a
# signal while enforce is off.

import os
import tempfile
from datetime import datetime

import db_utils
from vol1d import config as vol1d_config
from vol1d import regime as vol1d_regime
from vol1d import shadow
from vol1d.state import Vol1DState


MIDDAY = datetime(2026, 7, 8, 11, 0)


def _state(**over):
    base = dict(
        ts=MIDDAY, vix1d=18.0, exp_move_pts=71.4, exp_move_adj=60.7,
        vix1d_tod_z=-1.0, iv_rv_spread=2.0, vix1d_roc=0.1,
        vol_state="COMPRESSED", spiking=False,
        combined_regime="COMPRESSED/POS_GEX", confidence=1.0,
        grid_action=vol1d_regime.FADE, spot=6300.0, baseline_sessions=60,
        qa_residual=-0.3)
    base.update(over)
    return Vol1DState(**base)


def _sig(**over):
    # SPY at 630 with exp_move_adj 60.7 SPX pts ~ 0.96% -> ~6.07 SPY pts.
    base = dict(symbol="SPY", direction="CALL", price=630.0,
                signal_type="VWAP_MR", grade="A",
                und_call_t1=633.0, und_call_stop=627.0)
    base.update(over)
    return base


def _cfg(enforce=False):
    cfg = vol1d_config.get_config()
    cfg["enforce"] = enforce
    return cfg


def _db():
    return os.path.join(tempfile.mkdtemp(prefix="vol1d-sh-"), "state.db")


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

def test_coherent_fade_signal_allows():
    out = shadow.evaluate_signal(_sig(), _state(), _cfg())
    assert out["verdict"] == shadow.ALLOW


def test_target_beyond_exp_move_vetoes_in_fade_tape():
    # Target 12 pts away ~ 2 sigma of the 6.07-pt adjusted move.
    out = shadow.evaluate_signal(_sig(und_call_t1=642.0), _state(), _cfg())
    assert out["verdict"] == shadow.VETO
    assert any("beyond_exp_move" in r for r in out["reasons"])


def test_same_target_ok_outside_fade_corner():
    st = _state(grid_action=vol1d_regime.TREND, vol_state="EXPANSIVE",
                combined_regime="EXPANSIVE/NEG_GEX")
    out = shadow.evaluate_signal(_sig(signal_type="ORB", und_call_t1=642.0),
                                 st, _cfg())
    assert out["verdict"] == shadow.ALLOW


def test_stop_inside_noise_vetoes_everywhere():
    # Stop 0.6 pts away ~ 0.1 sigma << 0.25 floor.
    out = shadow.evaluate_signal(_sig(und_call_stop=629.4), _state(), _cfg())
    assert out["verdict"] == shadow.VETO
    assert any("inside_noise" in r for r in out["reasons"])


def test_spike_stands_aside_only_without_position():
    st = _state(spiking=True)
    out = shadow.evaluate_signal(_sig(), st, _cfg(), has_open_position=False)
    assert out["verdict"] == shadow.STAND_ASIDE
    out = shadow.evaluate_signal(_sig(), st, _cfg(), has_open_position=True)
    assert out["verdict"] != shadow.STAND_ASIDE


def test_opening_grab_window_stands_aside():
    st = _state(ts=datetime(2026, 7, 8, 9, 40))
    out = shadow.evaluate_signal(_sig(), st, _cfg())
    assert out["verdict"] == shadow.STAND_ASIDE
    assert out["reasons"] == ["opening_grab_window"]


def test_counter_routing_downweights():
    # Trend setup in the fade corner.
    out = shadow.evaluate_signal(_sig(signal_type="ORB"), _state(), _cfg())
    assert out["verdict"] == shadow.DOWNWEIGHT
    # Fade setup in the trend corner.
    st = _state(grid_action=vol1d_regime.TREND)
    out = shadow.evaluate_signal(_sig(signal_type="VWAP_MR"), st, _cfg())
    assert out["verdict"] == shadow.DOWNWEIGHT


def test_low_confidence_abstains_before_any_veto():
    st = _state(confidence=0.2, spiking=True)
    out = shadow.evaluate_signal(_sig(und_call_t1=642.0), st, _cfg())
    assert out["verdict"] == shadow.ABSTAIN


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------

def test_size_scales_inverse_to_z_and_clamps():
    g = _cfg()["gating"]
    assert shadow.size_mult(-1.0, g) == 1.25    # quiet -> bigger, capped
    assert shadow.size_mult(0.0, g) == 1.0
    assert shadow.size_mult(1.0, g) == 0.75
    assert shadow.size_mult(4.0, g) == g["size_min"]
    assert shadow.size_mult(None, g) == 1.0     # warmup sizes flat


# ---------------------------------------------------------------------------
# Shadow-vs-enforce
# ---------------------------------------------------------------------------

def test_shadow_mode_logs_but_never_filters():
    db = _db()
    sigs = [_sig(und_call_t1=642.0),            # would VETO
            _sig(symbol="QQQ")]                 # would DOWNWEIGHT (ORB? no: VWAP_MR->ALLOW)
    out = shadow.process_signals(sigs, _state(), cfg=_cfg(enforce=False),
                                 db_path=db)
    assert len(out) == 2, "shadow mode must not drop signals"
    conn = db_utils.connect(db)
    rows = conn.execute("SELECT verdict, enforced FROM vol1d_shadow").fetchall()
    conn.close()
    assert len(rows) == 2
    assert all(enforced == 0 for _, enforced in rows)
    assert rows[0][0] == shadow.VETO


def test_enforce_flip_filters_vetoes():
    db = _db()
    sigs = [_sig(und_call_t1=642.0), _sig(symbol="QQQ")]
    out = shadow.process_signals(sigs, _state(), cfg=_cfg(enforce=True),
                                 db_path=db)
    assert [s["symbol"] for s in out] == ["QQQ"]
    conn = db_utils.connect(db)
    rows = conn.execute(
        "SELECT symbol, enforced FROM vol1d_shadow ORDER BY id").fetchall()
    conn.close()
    assert rows[0] == ("SPY", 1)


def test_downweight_never_filters_even_enforced():
    db = _db()
    sigs = [_sig(signal_type="ORB")]            # DOWNWEIGHT
    out = shadow.process_signals(sigs, _state(), cfg=_cfg(enforce=True),
                                 db_path=db)
    assert len(out) == 1, "DOWNWEIGHT is advisory sizing, not a block"


def test_no_state_abstains_and_passes():
    db = _db()
    out = shadow.process_signals([_sig()], None, cfg=_cfg(enforce=True),
                                 db_path=db)
    assert len(out) == 1
