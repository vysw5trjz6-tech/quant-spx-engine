"""Structural tests for the EOD AI model routing and Friday digest hook.

We don't import main.py directly (it boots threads + init_db on import).
Instead we parse the source with ast and assert the relevant code is wired:
  - run_ai_improvement routes end_of_day to claude-opus-4-7
  - run_friday_digest exists, weekday-gated to Friday (weekday()==4), and
    sends via Telegram using Opus
  - background_scheduler triggers run_friday_digest at >= 16.25 ET
"""
import ast
import os

import pytest


MAIN_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "main.py")


@pytest.fixture(scope="module")
def main_src():
    with open(MAIN_PATH, "r", encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def main_tree(main_src):
    return ast.parse(main_src)


def _find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def test_run_ai_improvement_routes_eod_to_opus(main_tree, main_src):
    fn = _find_func(main_tree, "run_ai_improvement")
    assert fn is not None, "run_ai_improvement function missing"

    src = ast.get_source_segment(main_src, fn) or ""
    assert "claude-opus-4-7" in src, (
        "run_ai_improvement should reference claude-opus-4-7 for EOD"
    )
    assert "claude-sonnet-4-20250514" in src, (
        "run_ai_improvement should still reference Sonnet for non-EOD"
    )
    assert 'trigger == "end_of_day"' in src or "trigger=='end_of_day'" in src, (
        "EOD model selection must branch on trigger == 'end_of_day'"
    )


def test_run_friday_digest_exists_and_uses_opus(main_tree, main_src):
    fn = _find_func(main_tree, "run_friday_digest")
    assert fn is not None, "run_friday_digest function missing"

    src = ast.get_source_segment(main_src, fn) or ""
    # Friday-gated
    assert "weekday() != 4" in src, "Digest must early-return when not Friday"
    # Opus
    assert "claude-opus-4-7" in src, "Friday digest must use Opus 4.7"
    # Sends via Telegram
    assert "send_telegram(" in src, "Friday digest must dispatch via Telegram"
    # Calls Anthropic API
    assert "api.anthropic.com/v1/messages" in src


def test_scheduler_invokes_friday_digest(main_src):
    """The scheduler must wire run_friday_digest with a weekday + time guard."""
    assert "run_friday_digest" in main_src, (
        "Scheduler must reference run_friday_digest"
    )
    # The friday-digest scheduler block uses weekday() == 4 (Friday) and the
    # >=16.25 ET window. We check both anchors appear.
    assert "weekday() == 4" in main_src
    assert "16.25" in main_src
    assert 'friday_digest' in main_src


def test_filter_trades_since_helper(main_tree, main_src):
    """_filter_trades_since must keep rows with ts[:10] >= cutoff."""
    fn = _find_func(main_tree, "_filter_trades_since")
    assert fn is not None
    src = ast.get_source_segment(main_src, fn) or ""
    # Exec the helper in isolation against a tiny dataset.
    ns = {}
    exec(compile(src, "<filter>", "exec"), ns)
    filter_fn = ns["_filter_trades_since"]
    trades = [
        {"ts": "2026-05-10T14:00:00+00:00", "id": 1},  # before cutoff
        {"ts": "2026-05-13T14:00:00+00:00", "id": 2},  # on cutoff
        {"ts": "2026-05-15T19:30:00+00:00", "id": 3},  # after cutoff
        {"ts": "",                          "id": 4},  # empty -> excluded
        {"id": 5},                                      # missing -> excluded
    ]
    kept = filter_fn(trades, "2026-05-13")
    kept_ids = sorted(t["id"] for t in kept)
    assert kept_ids == [2, 3]


def test_daily_refresh_flag_tracks_friday(main_src):
    """Bootstrap must pre-mark friday_digest done on non-Fridays."""
    # Both bootstrap + date-roll branches reference 'friday_digest'
    assert main_src.count('"friday_digest"') >= 2
