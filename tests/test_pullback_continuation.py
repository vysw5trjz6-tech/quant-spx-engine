"""Behavioral tests for detect_orb_pullback_continuation.

On a clean trend day the anti-chase rule demotes every extended breakout, so
the engine goes dark exactly when the trend is strongest. The pullback
continuation detector is the one high-probability way back in: an extended
move that pulled back to the trigger, held it as a higher low, and is resuming
on volume. These tests pin that behavior down.

main.py boots background threads at import (repo convention: parse, don't
import), and the detector is fully self-contained, so we compile just that one
function into an isolated namespace and exercise it directly.
"""
import ast
import os

import pytest

MAIN_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "main.py")


def _find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


@pytest.fixture(scope="module")
def detect():
    with open(MAIN_PATH, "r", encoding="utf-8") as f:
        src = f.read()
    fn = _find_func(ast.parse(src), "detect_orb_pullback_continuation")
    assert fn is not None, "detect_orb_pullback_continuation missing"
    ns = {}
    exec(compile(ast.get_source_segment(src, fn), MAIN_PATH, "exec"), ns)
    return ns["detect_orb_pullback_continuation"]


CFG = {
    "pullback_min_depth":     0.25,
    "pullback_vol_min":       1.1,
    "max_breakout_extension": 0.6,
}

ORB_BARS = 6
ORB_HIGH = 100.0
ORB_LOW  = 99.0
ORB_RANGE = 1.0


def _bar(o, h, l, c):
    return {"o": o, "h": h, "l": l, "c": c, "v": 1000}


def _orb_prefix():
    # 6 ORB-window bars (content irrelevant; only count matters to the slice)
    return [_bar(99.5, 100.0, 99.0, 99.8) for _ in range(ORB_BARS)]


def test_valid_call_continuation_fires(detect):
    # Ran extended to ~101 (1.0x ORB past trigger), pulled back to 100.4
    # (held the 100.0 trigger as a higher low), now resuming green > VWAP.
    bars = _orb_prefix() + [
        _bar(100.05, 100.30, 100.0, 100.2),  # initial breakout bar
        _bar(100.2, 101.0, 100.1, 100.9),    # extension leg
        _bar(100.9, 101.0, 100.4, 100.5),    # pullback
        _bar(100.5, 100.95, 100.45, 100.9),  # resumption (green, > prev close)
    ]
    res = detect(bars, vwap=100.2, orb_high=ORB_HIGH, orb_low=ORB_LOW,
                 direction="CALL", orb_range=ORB_RANGE, cfg=CFG,
                 time_vol_ratio=1.5, vol_data_ok=True, orb_bars=ORB_BARS)
    assert res and res["ok"]
    # Stop sits just under the held pullback low (100.4 - 0.15*range).
    assert res["stop"] == pytest.approx(100.25, abs=0.01)


def test_still_ripping_at_highs_does_not_fire(detect):
    # No pullback off the high -- this is chasing, anti-chase must still win.
    bars = _orb_prefix() + [
        _bar(100.05, 100.30, 100.0, 100.2),
        _bar(100.2, 101.0, 100.1, 100.9),
        _bar(100.9, 101.6, 100.9, 101.5),
        _bar(101.5, 101.9, 101.5, 101.85),  # closing at the highs
    ]
    res = detect(bars, vwap=100.2, orb_high=ORB_HIGH, orb_low=ORB_LOW,
                 direction="CALL", orb_range=ORB_RANGE, cfg=CFG,
                 time_vol_ratio=1.5, vol_data_ok=True, orb_bars=ORB_BARS)
    assert res is None


def test_trigger_lost_does_not_fire(detect):
    # Pulled back THROUGH the trigger (low 99.7 < ORB-high 100.0): failed
    # breakout, not a continuation.
    bars = _orb_prefix() + [
        _bar(100.05, 100.30, 100.0, 100.2),
        _bar(100.2, 101.0, 100.1, 100.9),
        _bar(100.9, 101.0, 99.7, 99.9),     # broke back below trigger
        _bar(99.9, 100.3, 99.8, 100.2),
    ]
    res = detect(bars, vwap=100.2, orb_high=ORB_HIGH, orb_low=ORB_LOW,
                 direction="CALL", orb_range=ORB_RANGE, cfg=CFG,
                 time_vol_ratio=1.5, vol_data_ok=True, orb_bars=ORB_BARS)
    assert res is None


def test_low_volume_does_not_fire(detect):
    bars = _orb_prefix() + [
        _bar(100.05, 100.30, 100.0, 100.2),
        _bar(100.2, 101.0, 100.1, 100.9),
        _bar(100.9, 101.0, 100.4, 100.5),
        _bar(100.5, 100.95, 100.45, 100.9),
    ]
    res = detect(bars, vwap=100.2, orb_high=ORB_HIGH, orb_low=ORB_LOW,
                 direction="CALL", orb_range=ORB_RANGE, cfg=CFG,
                 time_vol_ratio=0.8, vol_data_ok=True, orb_bars=ORB_BARS)
    assert res is None
    # Same setup but volume data missing entirely -> sentinel must not pass.
    res2 = detect(bars, vwap=100.2, orb_high=ORB_HIGH, orb_low=ORB_LOW,
                  direction="CALL", orb_range=ORB_RANGE, cfg=CFG,
                  time_vol_ratio=1.5, vol_data_ok=False, orb_bars=ORB_BARS)
    assert res2 is None


def test_never_extended_does_not_fire(detect):
    # Price is above the trigger but the move never extended past
    # max_breakout_extension -- a fresh, shallow poke, not a trend leg.
    bars = _orb_prefix() + [
        _bar(100.0, 100.20, 100.0, 100.15),
        _bar(100.0, 100.25, 100.0, 100.2),
        _bar(100.2, 100.3, 100.05, 100.1),
        _bar(100.1, 100.25, 100.05, 100.2),
    ]
    res = detect(bars, vwap=100.0, orb_high=ORB_HIGH, orb_low=ORB_LOW,
                 direction="CALL", orb_range=ORB_RANGE, cfg=CFG,
                 time_vol_ratio=1.5, vol_data_ok=True, orb_bars=ORB_BARS)
    assert res is None


def test_valid_put_continuation_fires(detect):
    # Mirror image below the ORB low.
    bars = _orb_prefix() + [
        _bar(98.95, 99.0, 98.7, 98.8),      # initial breakdown bar
        _bar(98.8, 98.9, 98.0, 98.1),       # extension leg down to ~98.0
        _bar(98.1, 98.6, 98.0, 98.5),       # pullback up to 98.6 (< 99.0 low)
        _bar(98.5, 98.55, 98.05, 98.1),     # resumption (red, < prev close)
    ]
    res = detect(bars, vwap=98.8, orb_high=ORB_HIGH, orb_low=ORB_LOW,
                 direction="PUT", orb_range=ORB_RANGE, cfg=CFG,
                 time_vol_ratio=1.5, vol_data_ok=True, orb_bars=ORB_BARS)
    assert res and res["ok"]
    # Stop just above the held pullback high (98.6 + 0.15*range).
    assert res["stop"] == pytest.approx(98.75, abs=0.01)
