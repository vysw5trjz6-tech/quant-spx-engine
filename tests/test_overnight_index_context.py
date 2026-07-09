# Offline tests for the real-index premarket context in overnight_context:
# ratio anchoring into SPX points, premarket gap classes, the unit-mismatch
# fix in get_premarket_brief, and the plan_summary premarket fallback.

from datetime import date

import overnight_context
import plan_summary


def _on_data(first=6320.0, last=6350.0, high=6360.0, low=6310.0,
             source="futures"):
    rng = high - low
    return {
        "high": high, "low": low, "range": rng, "mid": (high + low) / 2,
        "first_print": first, "last_print": last,
        "close_loc": (last - low) / rng if rng else 0.5,
        "upper_v": 100.0, "middle_v": 100.0, "lower_v": 100.0,
        "bar_count": 12, "source": source,
    }


def _spx_prev(c=6300.0, h=6330.0, l=6260.0):
    return {"date": "2026-07-08", "o": 6280.0, "h": h, "l": l, "c": c,
            "source": "cboe"}


# =============================================
# index_premarket_context
# =============================================

def test_ratio_anchoring_into_index_points():
    # ES first print 6320 vs real SPX close 6300 -> ~20 pt basis folded out.
    ctx = overnight_context.index_premarket_context(_on_data(), _spx_prev())
    scale = 6300.0 / 6320.0
    assert ctx["prev_close"] == 6300.0
    assert abs(ctx["implied_open"] - 6350.0 * scale) < 0.01
    assert abs(ctx["on_high"] - 6360.0 * scale) < 0.01
    assert abs(ctx["on_low"] - 6310.0 * scale) < 0.01
    # gap_pct consistent with implied open
    expected_gap = (6350.0 * scale - 6300.0) / 6300.0 * 100
    assert abs(ctx["gap_pct"] - expected_gap) < 0.01


def test_premarket_class_above_prev_high():
    # Implied open ~6330.1 > prev high 6320
    ctx = overnight_context.index_premarket_context(
        _on_data(first=6320.0, last=6351.0),
        _spx_prev(c=6300.0, h=6320.0, l=6260.0))
    assert ctx["implied_open"] > 6320.0
    assert ctx["premarket_class"] == "ABOVE_PREV_HIGH"


def test_premarket_class_flat_and_inside():
    flat = overnight_context.index_premarket_context(
        _on_data(first=6320.0, last=6321.0),
        _spx_prev(c=6300.0, h=6330.0, l=6260.0))
    assert flat["premarket_class"] == "FLAT"

    inside = overnight_context.index_premarket_context(
        _on_data(first=6320.0, last=6340.0),
        _spx_prev(c=6300.0, h=6330.0, l=6260.0))
    assert inside["premarket_class"] == "INSIDE_PREV_RANGE"


def test_post_open_gap_classified_in_index_points():
    # SPY opened 632.0 vs SPY prev close 630.0 -> SPX-equivalent open 6320.
    ctx = overnight_context.index_premarket_context(
        _on_data(), _spx_prev(), rth_open=632.0, proxy_prev_close=630.0)
    assert abs(ctx["rth_open"] - 6320.0) < 0.01
    gap = ctx["gap"]
    assert gap is not None
    # 6320 sits inside the converted ON range (~6290.1 .. 6340.0)
    assert gap["class"] == "INSIDE_GAP"
    assert gap["direction"] == "UP"


def test_unusable_inputs_return_none():
    assert overnight_context.index_premarket_context(None, _spx_prev()) is None
    assert overnight_context.index_premarket_context(_on_data(), None) is None
    bad = _on_data()
    bad["first_print"] = 0
    assert overnight_context.index_premarket_context(bad, _spx_prev()) is None


# =============================================
# get_premarket_brief unit-safety
# =============================================

def _patch_overnight(monkeypatch, es=None, nq=None):
    def fake_range(target_date_et=None, contract="ES"):
        return es if contract == "ES" else nq
    monkeypatch.setattr(overnight_context, "overnight_range", fake_range)


def test_inventory_anchored_on_series_first_print(monkeypatch):
    es = _on_data()
    captured = {}
    real_inventory = overnight_context.overnight_inventory

    def spy_inventory(on_data, prev_rth_close):
        captured.setdefault("anchors", []).append(prev_rth_close)
        return real_inventory(on_data, prev_rth_close)

    _patch_overnight(monkeypatch, es=es, nq=None)
    monkeypatch.setattr(overnight_context, "overnight_inventory", spy_inventory)

    # SPY-units prev close (631.2) must NOT be compared against ES bars.
    brief = overnight_context.get_premarket_brief(prev_rth_close=631.2)
    assert captured["anchors"] == [es["first_print"]]
    assert brief["es_inventory"] is not None


def test_no_legacy_gap_when_futures_bars_and_no_index_data(monkeypatch):
    # Futures bars + SPY-unit open: comparing them was the old unit bug.
    _patch_overnight(monkeypatch, es=_on_data(source="futures"))
    brief = overnight_context.get_premarket_brief(
        prev_rth_close=631.2, rth_open=632.0)
    assert brief["gap"] is None


def test_legacy_gap_still_works_on_etf_proxy_path(monkeypatch):
    # SPY-unit bars + SPY-unit open: units match, legacy path allowed.
    es = _on_data(first=630.0, last=632.0, high=633.0, low=629.0,
                  source="etf_proxy")
    _patch_overnight(monkeypatch, es=es)
    brief = overnight_context.get_premarket_brief(
        prev_rth_close=630.5, rth_open=632.0)
    assert brief["gap"] is not None
    assert brief["gap"]["class"] == "INSIDE_GAP"


def test_brief_carries_index_context_and_post_open_gap(monkeypatch):
    _patch_overnight(monkeypatch, es=_on_data(), nq=_on_data(
        first=23100.0, last=23200.0, high=23250.0, low=23050.0))
    brief = overnight_context.get_premarket_brief(
        prev_rth_close=630.0, rth_open=632.0,
        spx_prev=_spx_prev(),
        ndx_prev={"date": "2026-07-08", "o": 23000.0, "h": 23150.0,
                  "l": 22900.0, "c": 23050.0, "source": "yahoo"})
    assert brief["spx"]["prev_close"] == 6300.0
    assert brief["ndx"]["prev_close"] == 23050.0
    assert brief["nq_inventory"] is not None
    # Legacy gap now comes from the unit-safe index classification
    assert brief["gap"] is not None
    assert brief["gap"] == brief["spx"]["gap"]


# =============================================
# plan_summary premarket fallback
# =============================================

def _base_regime():
    return {"regime": "NORMAL", "rules": {}}


def test_plan_uses_spx_premarket_class_when_gap_missing():
    premarket = {"gap": None,
                 "spx": {"premarket_class": "INSIDE_PREV_RANGE"}}
    plan = plan_summary.summarize_plan(_base_regime(), {}, premarket)
    assert "fill/fade likely" in plan

    premarket = {"gap": None,
                 "spx": {"premarket_class": "ABOVE_PREV_HIGH"}}
    plan = plan_summary.summarize_plan(_base_regime(), {}, premarket)
    assert "continuation watch" in plan


def test_plan_prefers_real_gap_over_premarket_class():
    premarket = {"gap": {"class": "INSIDE_GAP", "direction": "UP"},
                 "spx": {"premarket_class": "ABOVE_PREV_HIGH"}}
    plan = plan_summary.summarize_plan(_base_regime(), {}, premarket)
    assert "morning fade likely" in plan
    assert "continuation watch" not in plan
