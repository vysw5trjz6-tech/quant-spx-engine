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
# #4 -- chase-into-resistance demotion (behavioral + structural)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def blocker_chase_frac(main_src, main_tree):
    """Compile just _blocker_chase_frac into an isolated namespace."""
    fn = _find_func(main_tree, "_blocker_chase_frac")
    assert fn is not None, "_blocker_chase_frac missing"
    ns = {}
    exec(compile(ast.get_source_segment(main_src, fn), MAIN_PATH, "exec"), ns)
    return ns["_blocker_chase_frac"]


def _ca(blk_price, clear_to_t1=False):
    return {"clear_to_t1": clear_to_t1,
            "blocking_level": {"price": blk_price, "label": "1H-R"}}


def test_chase_frac_blocker_on_top_of_entry(blocker_chase_frac):
    # AMD from the logs: entry 525.79, blocker 526.47, T1 541.28 -> ~4% of path.
    frac = blocker_chase_frac(525.79, 541.28, _ca(526.47))
    assert frac is not None and frac < 0.34, "immediate-overhead blocker must trip the guard"


def test_chase_frac_blocker_far_along_path(blocker_chase_frac):
    # MU from the logs: entry 1021.65, blocker 1036.37, T1 1053.9 -> ~46% of path.
    frac = blocker_chase_frac(1021.65, 1053.9, _ca(1036.37))
    assert frac is not None and frac > 0.34, "a blocker near T1 must NOT trip the guard"


def test_chase_frac_none_when_clear(blocker_chase_frac):
    assert blocker_chase_frac(100.0, 105.0, _ca(101.0, clear_to_t1=True)) is None
    assert blocker_chase_frac(100.0, None, _ca(101.0)) is None
    assert blocker_chase_frac(100.0, 105.0, None) is None


def test_chase_demotion_wired_into_both_paths(main_src):
    # Both the ORB and alt-strategy assembly paths must demote on a near blocker.
    assert main_src.count("chase-into-resistance -> D") == 2, \
        "chase guard must demote to D in both signal-assembly paths"
    assert "\"chase_resist_frac\"" in main_src


# ---------------------------------------------------------------------------
# #5 -- score normalized to a common 0-100 scale
# ---------------------------------------------------------------------------

def test_scores_clamped_to_0_100(main_src):
    # Every intraday generator must clamp onto the shared 0-100 band so the
    # `score` field is comparable across signal types.
    assert main_src.count("min(100.0, score * vol_mult)") >= 4, \
        "ORB + alt-strategy scores must all clamp to a common 0-100 scale"
    # The old raw ORB formula (~2 scale) must be gone.
    assert "(breakout_strength * 100 + vol_ratio) * vol_mult" not in main_src


# ---------------------------------------------------------------------------
# #6 -- overpriced-option (IV) guardrail
# ---------------------------------------------------------------------------

def test_iv_cap_configured(main_src):
    assert "\"max_option_iv\"" in main_src


def test_option_picker_accepts_and_enforces_iv_cap(main_src, main_tree):
    fn = _find_func(main_tree, "get_liquid_option")
    assert fn is not None, "get_liquid_option missing"
    assert "max_iv" in [a.arg for a in fn.args.args], \
        "get_liquid_option must accept a max_iv cap"
    body = ast.get_source_segment(main_src, fn)
    assert "IV_HIGH" in body and "best[\"iv\"] > max_iv" in body, \
        "picker must reject the selected contract when its IV tops the cap"


def test_iv_cap_passed_by_intraday_callers(main_src):
    assert main_src.count("max_iv=cfg.get(\"max_option_iv\")") == 2, \
        "both intraday callers must pass the configured IV cap"
    assert main_src.count("IV>{:.0f}% (overpriced)") == 2, \
        "both callers must surface the overpriced-IV skip reason"


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
