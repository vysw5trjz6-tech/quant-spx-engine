"""Regression tests for the AI stats-labeling fixes.

The AI improvement loop tunes the scanner from _build_stats_summary(). Three
labeling defects were feeding it fiction:

1. Missing entry_hour defaulted to 9.5 -> every paper trade counted in the
   9:30-10:00 bucket ("eliminate trades after 11 AM" was based on this).
2. Missing rs defaulted to 0 -> every paper trade counted as rs_negative.
3. Missing aligned defaulted to True -> counter_trend group was always empty.

Additionally weekly swing signals carried no grade, so all weekly trades
grouped under grade "?" -- producing the "'?' outperforms every real grade"
insight. Missing labels must land in explicit unknown buckets, and the
summary must expose the WEEKLY/INTRADAY split (by_horizon).
"""
import main


def _trade(**kw):
    t = {"symbol": "SPY", "direction": "CALL", "outcome": "WIN",
         "r_mult": 1.0, "grade": "B", "grade_pts": 60, "gap_pct": 0.2,
         "gap_dir": "UP", "rs": 1.0, "entry_hour": 9.75,
         "ts": "2026-07-09T14:00:00+00:00", "signal_type": "ORB",
         "horizon": "INTRADAY"}
    t.update(kw)
    return t


def test_missing_entry_hour_goes_to_unknown_not_morning_bucket():
    stats = main._build_stats_summary([_trade(entry_hour=None)])
    assert "unknown" in stats["by_time"]
    assert "9:30-10:00" not in stats["by_time"]


def test_real_entry_hour_still_bucketed():
    stats = main._build_stats_summary([_trade(entry_hour=13.5)])
    assert "1:00-2:00" in stats["by_time"]


def test_missing_rs_goes_to_rs_unknown():
    stats = main._build_stats_summary([_trade(rs=None)])
    assert "rs_unknown" in stats["by_rs"]
    assert "rs_negative" not in stats["by_rs"]


def test_missing_aligned_goes_to_unknown_not_aligned():
    t = _trade()
    t.pop("rs")  # not relevant here
    # closed-trade rows never carry an 'aligned' key
    stats = main._build_stats_summary([t])
    assert "unknown" in stats["by_alignment"]
    assert "aligned" not in stats["by_alignment"]


def test_by_horizon_split_present():
    stats = main._build_stats_summary([
        _trade(horizon="INTRADAY"),
        _trade(horizon="WEEKLY", outcome="LOSS"),
    ])
    assert stats["by_horizon"]["INTRADAY"]["wins"] == 1
    assert stats["by_horizon"]["WEEKLY"]["losses"] == 1


def test_weekly_rs_override_in_baseline_config():
    assert main.DEFAULT_CONFIG.get("weekly_rs_override") == 5.0


def test_training_floor_covers_labeling_fix():
    # Trades logged before the labeling fixes carry fabricated buckets and
    # must stay out of AI tuning.
    assert main.AI_TRAINING_FLOOR >= "2026-07-09T00:00:00+00:00"
