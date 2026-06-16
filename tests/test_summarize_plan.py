"""Tests for the morning-brief plan-text composer.

The pre-fix version produced contradictions like
    'Range-bound day -- fade extremes only. ORB favored.'
when regime label and GEX tape disagreed. These tests pin the conflict-
resolution behavior.
"""
import os
import sys


def _summarize_plan():
    from plan_summary import summarize_plan
    return summarize_plan


def test_no_conflict_compressed_with_mean_revert_gex_is_clean():
    plan = _summarize_plan()(
        regime  = {"regime": "COMPRESSED", "expansion_watch": False},
        gex_bias= {"tape_bias": "MEAN_REVERT"},
        premarket={},
    )
    assert "Range-bound" in plan
    assert "VWAP fades favored" in plan
    # Sanity: no contradicting trend phrase
    assert "ORB favored" not in plan


def test_conflict_compressed_with_trend_gex_emits_mixed_signal():
    """The 2026-05-13 bug: COMPRESSED regime + TREND GEX produced two
    opposite instructions on the same line. After fix it must be a single
    'mixed signals' line, not a stacked contradiction."""
    plan = _summarize_plan()(
        regime  = {"regime": "COMPRESSED", "expansion_watch": False},
        gex_bias= {"tape_bias": "TREND"},
        premarket={},
    )
    assert "Mixed signals" in plan
    # The buggy combination must not appear together
    assert not ("fade extremes only" in plan and "ORB favored" in plan)


def test_expansion_watch_explicitly_overrides_compressed_text():
    plan = _summarize_plan()(
        regime  = {
            "regime":         "COMPRESSED",
            "expansion_watch": True,
            "term_structure": {"label": "CONTANGO"},
        },
        gex_bias= {"tape_bias": "MIXED"},
        premarket={},
    )
    assert "EXPANSION_WATCH" in plan
    assert "fade extremes only" not in plan


def test_intraday_flip_overrides_morning_label():
    plan = _summarize_plan()(
        regime  = {"regime": "LOW_VOL", "intraday_flip": True},
        gex_bias= {"tape_bias": "MIXED"},
        premarket={},
    )
    assert "Intraday regime flip" in plan


def test_gap_none_does_not_crash():
    """Production regression: get_premarket_brief() sets gap=None (not absent)
    when RTH-open/overnight ES data is unavailable. The brief thread crashed
    with 'NoneType has no attribute get' before send_telegram, so no premarket
    brief and no alerts went out. A None gap must be treated as 'no gap data'."""
    plan = _summarize_plan()(
        regime  = {"regime": "NORMAL"},
        gex_bias= {"tape_bias": "MIXED"},
        premarket={"gap": None, "es_overnight": None},
    )
    assert plan.endswith(".")
    assert "morning fade likely" not in plan
    assert "continuation likely" not in plan


def test_term_structure_backwardation_adds_sidenote():
    plan = _summarize_plan()(
        regime  = {
            "regime":         "NORMAL",
            "term_structure": {"label": "BACKWARDATION"},
        },
        gex_bias= {"tape_bias": "MIXED"},
        premarket={},
    )
    assert "backwardated" in plan
