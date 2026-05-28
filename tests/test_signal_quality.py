"""Regression tests for the signal-quality fixes:

  #1 Swing engine resilience  -- cached, rate-limit-aware bar fetch + a loud
     whole-universe outage summary instead of a silent data:0/72.
  #2 VWAP volume N/A guard     -- the 1.0 "N/A" volume sentinel must NOT pass
     the volume gate on volume-confirmed strategies.
  #3 Clear-air proximity cap   -- levels within a tolerance band of price no
     longer block T1, and a weak (1H) level applies a softer penalty than the
     hard C cap reserved for stronger (4hr/daily) levels.

main.py boots background threads + init_db at import, so (per the repo
convention in test_friday_digest.py) we parse the source rather than import
it. check_clear_air is fully self-contained, so we extract just that function
and exercise it for a true behavioral test.
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


@pytest.fixture(scope="module")
def check_clear_air(main_src, main_tree):
    """Compile just check_clear_air into an isolated namespace."""
    fn = _find_func(main_tree, "check_clear_air")
    assert fn is not None, "check_clear_air missing"
    ns = {}
    exec(compile(ast.get_source_segment(main_src, fn), MAIN_PATH, "exec"), ns)
    return ns["check_clear_air"]


# ---------------------------------------------------------------------------
# #3 -- clear-air tolerance + weak-level handling (behavioral)
# ---------------------------------------------------------------------------

def test_near_touching_level_does_not_block_with_tolerance(check_clear_air):
    # Resistance only 0.08% overhead -- within noise, should be ignored.
    price = 754.30
    near  = price * 1.0008  # ~0.08% away
    levels = [{"price": near, "label": "1H-R", "tf": "1hr", "strength": 1}]
    t1, t2 = price * 1.005, price * 1.01

    blocked = check_clear_air(price, "CALL", t1, t2, levels, tol_pct=0.0)
    assert blocked["clear_to_t1"] is False, "baseline: level should block"

    cleared = check_clear_air(price, "CALL", t1, t2, levels, tol_pct=0.12)
    assert cleared["clear_to_t1"] is True, "level inside tolerance must not block"


def test_real_level_still_blocks_with_tolerance(check_clear_air):
    # A level comfortably outside the tolerance band still blocks T1.
    price = 100.0
    levels = [{"price": 100.4, "label": "1H-R", "tf": "1hr", "strength": 1}]
    t1, t2 = 101.0, 102.0
    res = check_clear_air(price, "CALL", t1, t2, levels, tol_pct=0.12)
    assert res["clear_to_t1"] is False
    assert res["blocking_level"]["price"] == 100.4


def test_tolerance_applies_to_puts(check_clear_air):
    price = 100.0
    near  = price * (1 - 0.0008)  # support 0.08% below
    levels = [{"price": near, "label": "1H-S", "tf": "1hr", "strength": 1}]
    t1, t2 = 99.5, 99.0
    res = check_clear_air(price, "PUT", t1, t2, levels, tol_pct=0.12)
    assert res["clear_to_t1"] is True


def test_blocking_level_strength_surfaced(check_clear_air):
    # The cap logic needs the blocking level's strength to decide weak vs hard.
    price = 100.0
    levels = [{"price": 100.4, "label": "1H-R", "tf": "1hr", "strength": 1}]
    res = check_clear_air(price, "CALL", 101.0, 102.0, levels, tol_pct=0.0)
    assert res["blocking_level"]["strength"] == 1


# ---------------------------------------------------------------------------
# #3 -- cap sites distinguish weak levels (structural)
# ---------------------------------------------------------------------------

def test_clear_air_called_with_tolerance(main_src):
    assert "tol_pct=cfg.get(\"clear_air_tol_pct\"" in main_src, \
        "check_clear_air call sites must pass the configured tolerance"


def test_weak_level_softer_penalty(main_src):
    assert "clear_air_weak_strength" in main_src
    # Weak path caps points higher (62) than the hard C cap (52).
    assert "min(grade_pts, 62)" in main_src, \
        "weak blocking level should use the softer 62-pt ceiling"


def test_clear_air_tol_default_configured(main_src):
    assert "\"clear_air_tol_pct\"" in main_src


# ---------------------------------------------------------------------------
# #2 -- VWAP volume N/A guard (structural)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn_name", [
    "detect_vwap_trend",
    "detect_vwap_mean_reversion",
    "detect_ib_extension",
])
def test_volume_strategies_accept_vol_data_ok(main_tree, fn_name):
    fn = _find_func(main_tree, fn_name)
    assert fn is not None, "{} missing".format(fn_name)
    arg_names = [a.arg for a in fn.args.args]
    assert "vol_data_ok" in arg_names, \
        "{} must accept vol_data_ok".format(fn_name)


@pytest.mark.parametrize("fn_name", [
    "detect_vwap_trend",
    "detect_vwap_mean_reversion",
    "detect_ib_extension",
])
def test_volume_strategies_guard_on_na(main_src, main_tree, fn_name):
    fn = _find_func(main_tree, fn_name)
    body_src = ast.get_source_segment(main_src, fn)
    assert "if not vol_data_ok:" in body_src, \
        "{} must bail when volume data is unavailable".format(fn_name)


def test_scan_computes_and_passes_vol_data_ok(main_src):
    assert "vol_data_ok = tv_lbl != \"N/A\"" in main_src, \
        "scan must derive vol_data_ok from the N/A volume label"
    assert main_src.count("vol_data_ok=vol_data_ok") >= 3, \
        "all three volume strategies must receive vol_data_ok"


# ---------------------------------------------------------------------------
# #1 -- swing fetch resilience (structural)
# ---------------------------------------------------------------------------

def test_fetch_bars_helper_exists(main_tree):
    assert _find_func(main_tree, "_fetch_bars") is not None
    assert _find_func(main_tree, "_bars_cache_get") is not None
    assert _find_func(main_tree, "_bars_cache_set") is not None


def test_fetch_bars_has_rate_limit_handling(main_src, main_tree):
    fn = _find_func(main_tree, "_fetch_bars")
    body = ast.get_source_segment(main_src, fn)
    assert "429" in body, "must special-case the rate-limit status"
    assert "time.sleep(backoff)" in body, "must back off between retries"
    assert "http_{}" in body, "must record HTTP status into swing stats"
    assert "stale" in body.lower(), "must fall back to stale cache on failure"


def test_getters_delegate_to_fetch_bars(main_src, main_tree):
    for name, tf in [("get_daily_extended", "1Day"), ("get_weekly_bars", "1Week")]:
        fn = _find_func(main_tree, name)
        body = ast.get_source_segment(main_src, fn)
        assert "_fetch_bars(symbol, \"{}\"".format(tf) in body, \
            "{} should delegate to _fetch_bars".format(name)


def test_swing_scan_reports_outage(main_src):
    assert "OUTAGE" in main_src, "0/N swing scan must log a loud outage line"
    assert "dominant status" in main_src, \
        "outage line should name the dominant HTTP status"
