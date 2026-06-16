"""Tests for bar_utils: corrupt-print filtering and weekly aggregation.

The free IEX daily feed occasionally emits a bad bar (zero, inverted high/low,
out-of-range open/close, or a mis-decimaled close). One such bar silently
corrupts relative strength, the swing detectors, Fib levels and stop/target
math -- the KLAC -92% 5-day RS that gated the whole board on 2026-06-16 came
from a single bad close. Weekly aggregation rolls Databento's clean daily bars
into ISO-week buckets (Databento has no native weekly schema).
"""
from bar_utils import sanitize_bars, aggregate_weekly


def _bar(o, h, l, c, v=1_000_000, t="2026-06-10T00:00:00Z"):
    return {"o": o, "h": h, "l": l, "c": c, "v": v, "t": t}


def test_clean_series_is_untouched():
    bars = [_bar(100, 101, 99, 100.5),
            _bar(100.5, 102, 100, 101.5),
            _bar(101.5, 103, 101, 102.5)]
    assert sanitize_bars("TEST", list(bars), "daily") == bars


def test_drops_nonpositive_and_inverted_bars():
    bars = [_bar(100, 101, 99, 100.5),
            _bar(0, 0, 0, 0),                # zero print
            _bar(100, 99, 101, 100),         # high < low
            _bar(101, 103, 100, 102)]
    out = sanitize_bars("TEST", bars, "daily")
    assert len(out) == 2
    assert all(b["c"] > 0 and b["h"] >= b["l"] for b in out)


def test_drops_open_close_outside_range():
    bars = [_bar(100, 101, 99, 100),
            _bar(100, 101, 99, 150),         # close above the high
            _bar(100, 101, 99, 100)]
    out = sanitize_bars("TEST", bars, "daily")
    assert len(out) == 2


def test_drops_single_bar_spike_that_reverts():
    """The KLAC signature: one close ~1/12 of its neighbors, neighbors agree."""
    bars = [_bar(1000, 1010, 990, 1000),
            _bar(80, 90, 78, 85),            # bad decimal: spikes down, reverts
            _bar(1000, 1015, 995, 1005)]
    out = sanitize_bars("KLAC", bars, "daily")
    assert len(out) == 2
    assert [b["c"] for b in out] == [1000, 1005]


def test_keeps_real_gap_that_persists():
    """A genuine gap stays at the new level on the next bar, so it must survive
    even though it's a large move -- only reverting spikes are corruption."""
    bars = [_bar(100, 101, 99, 100),
            _bar(70, 72, 68, 70),            # -30% gap down...
            _bar(70, 73, 69, 71)]            # ...that holds
    out = sanitize_bars("GAP", bars, "daily")
    assert len(out) == 3


def test_logger_called_only_when_bars_dropped():
    seen = []
    sanitize_bars("TEST", [_bar(100, 101, 99, 100)], "daily", log=seen.append)
    assert seen == []
    sanitize_bars("TEST", [_bar(0, 0, 0, 0)], "daily", log=seen.append)
    assert len(seen) == 1 and "dropped" in seen[0]


# ----------------------------- weekly aggregation ---------------------------

def _d(date, o, h, l, c, v=1000):
    return {"o": o, "h": h, "l": l, "c": c, "v": v, "t": date + "T00:00:00Z"}


def test_weekly_rolls_up_one_iso_week():
    # 2026-06-08 (Mon) .. 2026-06-12 (Fri) are one ISO week.
    daily = [_d("2026-06-08", 100, 105, 99, 101),
             _d("2026-06-09", 101, 108, 100, 104),
             _d("2026-06-10", 104, 106, 95, 96),
             _d("2026-06-11", 96, 99, 94, 98),
             _d("2026-06-12", 98, 110, 97, 109)]
    weeks = aggregate_weekly(daily)
    assert len(weeks) == 1
    w = weeks[0]
    assert w["o"] == 100          # first day's open
    assert w["c"] == 109          # last day's close
    assert w["h"] == 110          # max high
    assert w["l"] == 94           # min low
    assert w["v"] == 5000         # summed volume


def test_weekly_splits_across_week_boundary():
    daily = [_d("2026-06-11", 96, 99, 94, 98),     # week 24
             _d("2026-06-12", 98, 110, 97, 109),   # week 24
             _d("2026-06-15", 109, 112, 108, 111)]  # week 25 (next Monday)
    weeks = aggregate_weekly(daily)
    assert len(weeks) == 2
    assert weeks[0]["c"] == 109
    assert weeks[1]["o"] == 109
    assert weeks[1]["c"] == 111


def test_weekly_skips_unparseable_timestamps():
    daily = [_d("2026-06-08", 100, 105, 99, 101),
             {"o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "t": "garbage"}]
    weeks = aggregate_weekly(daily)
    assert len(weeks) == 1
    assert weeks[0]["c"] == 101
