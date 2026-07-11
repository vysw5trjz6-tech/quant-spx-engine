# Offline tests for vol1d/features.py: expected-move math (incl. the
# deliberate risk-premium shrink), the Parkinson RV estimator, ROC
# windowing, and the session reset.

import math
from datetime import datetime, timedelta

from vol1d import config as vol1d_config
from vol1d import features


def _cfg(rp=0.85, rv_window=30, roc_min=10):
    cfg = vol1d_config.get_config()
    cfg["features"]["rp_factor"] = rp
    cfg["features"]["rv_window_min"] = rv_window
    cfg["regime"]["roc_window_min"] = roc_min
    return cfg


def test_exp_move_formula_and_shrink():
    # 6300 * (20/100) / sqrt(252) = 79.37 pts; adj = pts * 0.85.
    pts, adj = features.exp_move(6300.0, 20.0, _cfg(rp=0.85))
    assert abs(pts - 6300 * 0.20 / math.sqrt(252)) < 0.01
    assert abs(adj - pts * 0.85) < 0.01
    assert adj < pts, "exp_move_adj must be the SHRUNK number"
    assert features.exp_move(None, 20.0) == (None, None)
    assert features.exp_move(6300.0, 0) == (None, None)


def test_parkinson_closed_form():
    # Every bar has ln(H/L) = x -> annualized vol = 100*x*sqrt(ann/(4 ln 2)).
    x = 0.001
    bars = [{"h": 100 * math.exp(x), "l": 100.0} for _ in range(20)]
    ann = 252 * 390
    expected = 100.0 * x * math.sqrt(ann / (4 * math.log(2)))
    got = features.parkinson_vol(bars, ann_minutes=ann)
    assert abs(got - expected) < 0.01


def test_parkinson_rejects_bad_bars():
    assert features.parkinson_vol([]) is None
    assert features.parkinson_vol([{"h": 100, "l": 101}] * 5) is None  # h < l
    assert features.parkinson_vol([{"h": 100, "l": 100}] * 2) == 0.0   # flat


def _run(tracker, start, steps, spot_fn, level_fn, step_s=15):
    out = None
    for i in range(steps):
        ts = start + timedelta(seconds=i * step_s)
        out = tracker.update(ts, spot_fn(i), level_fn(i))
    return out


def test_roc_measures_window_change():
    tracker = features.IntradayFeatures(_cfg(roc_min=10))
    start = datetime(2026, 7, 8, 10, 0)
    # Level rises 0.02/pass for 12.5 minutes -> ROC over ~10 min ~ +0.8.
    out = _run(tracker, start, 50, lambda i: 6300.0,
               lambda i: 15.0 + 0.02 * i)
    assert out["vix1d_roc"] is not None
    assert 0.6 <= out["vix1d_roc"] <= 1.0


def test_roc_none_until_half_window_of_history():
    tracker = features.IntradayFeatures(_cfg(roc_min=10))
    start = datetime(2026, 7, 8, 10, 0)
    out = _run(tracker, start, 4, lambda i: 6300.0, lambda i: 15.0)  # ~1 min
    assert out["vix1d_roc"] is None


def test_rv_and_spread_from_wiggling_spot():
    tracker = features.IntradayFeatures(_cfg())
    start = datetime(2026, 7, 8, 10, 0)
    # Spot oscillates +/-3 pts within each minute -> nonzero Parkinson RV.
    out = _run(tracker, start, 12 * 4,
               lambda i: 6300.0 + (3.0 if i % 2 else -3.0),
               lambda i: 18.0)
    assert out["rv_intraday"] is not None and out["rv_intraday"] > 0
    assert out["iv_rv_spread"] == round(18.0 - out["rv_intraday"], 3)


def test_session_roll_clears_history():
    tracker = features.IntradayFeatures(_cfg(roc_min=10))
    day1 = datetime(2026, 7, 8, 15, 55)
    _run(tracker, day1, 60, lambda i: 6300.0, lambda i: 20.0)
    # Next session opens: ROC must not measure against yesterday's levels.
    out = tracker.update(datetime(2026, 7, 9, 9, 30), 6310.0, 15.0)
    assert out["vix1d_roc"] is None
    assert out["rv_intraday"] is None
