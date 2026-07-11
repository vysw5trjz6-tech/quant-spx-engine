# Tests for the intraday regime de-risk / unlock recheck.
#
# The morning regime is set once; this recheck adapts it to the live tape.
# Coverage:
#   * a vol spike escalates a calm regime UP (ELEVATED -> MR off; CRISIS ->
#     swings off too)
#   * escalation is monotonic -- it never relaxes intraday and never re-fires
#     for a regime already reached
#   * the COMPRESSED -> LOW_VOL coiled-spring unlock still fires once
#   * a quiet tape, or an expansion that clears the ratio but not the absolute
#     floor, changes nothing

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main
import regime_filter as rf


def _setup(monkeypatch, label, rv20, rv_intra):
    monkeypatch.setattr(main, "HAS_REGIME", True)
    monkeypatch.setattr(main, "send_telegram", lambda *a, **k: None)
    monkeypatch.setattr(main, "_intraday_rv_spy", lambda: rv_intra)
    monkeypatch.setattr(main, "_regime_recheck_done",
                        {"date": None, "unlocked": False, "escalated_rank": -1})
    with main._market_state_lock:
        main._market_state["regime"] = {
            "regime":   label,
            "realized": rv20,
            "rules":    dict(rf.REGIME_STRATEGY_RULES[label]),
        }


def _live_regime():
    with main._market_state_lock:
        return dict(main._market_state.get("regime") or {})


def test_normal_vol_spike_escalates_to_elevated(monkeypatch):
    # 22% intraday vs 12% RV20: ratio 1.83 > 1.5 and >= 18% floor -> ELEVATED.
    _setup(monkeypatch, "NORMAL", rv20=12.0, rv_intra=22.0)
    main._intraday_regime_recheck()
    reg = _live_regime()
    assert reg["regime"] == "ELEVATED"
    assert reg["rules"]["vwap_mr"] is False          # mean reversion off
    assert reg["rules"]["conviction_multiplier"] == 0.85
    assert reg.get("intraday_flip") is True


def test_extreme_vol_spike_escalates_to_crisis(monkeypatch):
    # 30% vs 12%: ratio 2.5 > 2.0 and >= 26% floor -> CRISIS.
    _setup(monkeypatch, "NORMAL", rv20=12.0, rv_intra=30.0)
    main._intraday_regime_recheck()
    reg = _live_regime()
    assert reg["regime"] == "CRISIS"
    assert reg["rules"]["vwap_mr"] is False


def test_escalation_is_monotonic_no_downgrade(monkeypatch):
    # Escalate to CRISIS, then a milder (ELEVATED-level) read must not relax it.
    _setup(monkeypatch, "NORMAL", rv20=12.0, rv_intra=30.0)
    main._intraday_regime_recheck()
    assert _live_regime()["regime"] == "CRISIS"

    monkeypatch.setattr(main, "_intraday_rv_spy", lambda: 20.0)  # ELEVATED-ish
    main._intraday_regime_recheck()
    assert _live_regime()["regime"] == "CRISIS"       # still CRISIS, no downgrade


def test_already_elevated_does_not_re_escalate_same_level(monkeypatch):
    _setup(monkeypatch, "ELEVATED", rv20=12.0, rv_intra=22.0)
    main._intraday_regime_recheck()
    # Target ELEVATED == current ELEVATED -> no change, MR stays off as set.
    assert _live_regime()["regime"] == "ELEVATED"


def test_compressed_unlocks_to_low_vol_once(monkeypatch):
    # 11% vs 8%: ratio 1.375 > 1.3 but below the 18% de-risk floor -> unlock.
    _setup(monkeypatch, "COMPRESSED", rv20=8.0, rv_intra=11.0)
    main._intraday_regime_recheck()
    assert _live_regime()["regime"] == "LOW_VOL"

    # Manually shove it back to COMPRESSED; the one-shot guard must hold.
    with main._market_state_lock:
        main._market_state["regime"]["regime"] = "COMPRESSED"
    main._intraday_regime_recheck()
    assert _live_regime()["regime"] == "COMPRESSED"


def test_quiet_tape_changes_nothing(monkeypatch):
    _setup(monkeypatch, "NORMAL", rv20=11.0, rv_intra=12.0)
    main._intraday_regime_recheck()
    assert _live_regime()["regime"] == "NORMAL"


def test_ratio_met_but_absolute_floor_not_met(monkeypatch):
    # 17% vs 8%: ratio 2.1 clears 1.5 but 17 < 18 floor -> no de-risk.
    _setup(monkeypatch, "NORMAL", rv20=8.0, rv_intra=17.0)
    main._intraday_regime_recheck()
    assert _live_regime()["regime"] == "NORMAL"
