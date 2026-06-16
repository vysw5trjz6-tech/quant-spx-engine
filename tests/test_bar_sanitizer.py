"""Tests for _sanitize_bars: corrupt-print filtering on the daily/weekly feed.

The free IEX daily feed occasionally emits a bad bar (zero, inverted high/low,
out-of-range open/close, or a mis-decimaled close). One such bar silently
corrupts relative strength, the swing detectors, Fib levels and stop/target
math -- the KLAC -92% 5-day RS that gated the whole board on 2026-06-16 came
from a single bad close. These pin the filter's behavior.
"""
import main


def _bar(o, h, l, c, v=1_000_000, t="2026-06-10T00:00:00Z"):
    return {"o": o, "h": h, "l": l, "c": c, "v": v, "t": t}


def test_clean_series_is_untouched():
    bars = [_bar(100, 101, 99, 100.5),
            _bar(100.5, 102, 100, 101.5),
            _bar(101.5, 103, 101, 102.5)]
    assert main._sanitize_bars("TEST", list(bars), "daily") == bars


def test_drops_nonpositive_and_inverted_bars():
    bars = [_bar(100, 101, 99, 100.5),
            _bar(0, 0, 0, 0),                # zero print
            _bar(100, 99, 101, 100),         # high < low
            _bar(101, 103, 100, 102)]
    out = main._sanitize_bars("TEST", bars, "daily")
    assert len(out) == 2
    assert all(b["c"] > 0 and b["h"] >= b["l"] for b in out)


def test_drops_open_close_outside_range():
    bars = [_bar(100, 101, 99, 100),
            _bar(100, 101, 99, 150),         # close above the high
            _bar(100, 101, 99, 100)]
    out = main._sanitize_bars("TEST", bars, "daily")
    assert len(out) == 2


def test_drops_single_bar_spike_that_reverts():
    """The KLAC signature: one close ~1/12 of its neighbors, neighbors agree."""
    bars = [_bar(1000, 1010, 990, 1000),
            _bar(80, 90, 78, 85),            # bad decimal: spikes down, reverts
            _bar(1000, 1015, 995, 1005)]
    out = main._sanitize_bars("KLAC", bars, "daily")
    assert len(out) == 2
    assert [b["c"] for b in out] == [1000, 1005]


def test_keeps_real_gap_that_persists():
    """A genuine gap stays at the new level on the next bar, so it must survive
    even though it's a large move -- only reverting spikes are corruption."""
    bars = [_bar(100, 101, 99, 100),
            _bar(70, 72, 68, 70),            # -30% gap down...
            _bar(70, 73, 69, 71)]            # ...that holds
    out = main._sanitize_bars("GAP", bars, "daily")
    assert len(out) == 3
