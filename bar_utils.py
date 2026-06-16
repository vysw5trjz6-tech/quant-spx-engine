# bar_utils.py
# Pure OHLC-bar helpers shared by main.py and regime_filter.py.
#
# Kept dependency-free (no Flask, no scheduler, no network) so it can be unit
# tested in isolation and imported from regime_filter without creating an
# import cycle through main.

from datetime import datetime


# Largest single-bar close-to-close move we'll accept as real before treating
# a fully-reverting print as corrupt. Daily moves past this that revert in one
# bar are virtually always bad ticks (the free IEX feed prints these), not
# tradable events.
BAR_SPIKE_FRACTION = 0.35


def sanitize_bars(symbol, bars, kind, log=None):
    """Drop structurally-invalid and single-print spike bars from an OHLC
    series before it reaches the cache and the detectors.

    A single bad print -- a zero, an inverted high/low, an open/close outside
    the bar's own range, or a mis-decimaled close -- silently corrupts
    everything built on the series: relative strength (KLAC showed a -92% 5-day
    RS off one bad close, which gated the whole board as "counter-trend"), the
    O'Neil/Wyckoff/52w detectors, ATR, Fibonacci levels and stop/target math.

    Conservative by design: structural checks only drop bars that are
    internally impossible, and the spike check only drops a bar whose close
    disagrees sharply with BOTH neighbors while the neighbors agree with each
    other (the signature of a one-bar bad tick). A real gap persists into the
    next bar, so it survives.

    `log` is an optional callable(str); when provided it receives one line per
    fetch that dropped bars.
    """
    if not bars:
        return bars

    clean = []
    dropped = 0
    for b in bars:
        o, h, l, c = b.get("o"), b.get("h"), b.get("l"), b.get("c")
        if None in (o, h, l, c) or o <= 0 or h <= 0 or l <= 0 or c <= 0:
            dropped += 1
            continue
        if h < l:
            dropped += 1
            continue
        # Open/close must sit within the bar's own high/low (tiny float slack).
        lo, hi = l * 0.999, h * 1.001
        if not (lo <= o <= hi and lo <= c <= hi):
            dropped += 1
            continue
        clean.append(b)

    despiked = []
    for i, b in enumerate(clean):
        if 0 < i < len(clean) - 1:
            prev_c, next_c, cur_c = clean[i - 1]["c"], clean[i + 1]["c"], b["c"]
            if prev_c > 0 and next_c > 0:
                neighbors_agree = abs(next_c / prev_c - 1) < 0.12
                jumps_both = (abs(cur_c / prev_c - 1) > BAR_SPIKE_FRACTION
                              and abs(cur_c / next_c - 1) > BAR_SPIKE_FRACTION)
                if neighbors_agree and jumps_both:
                    dropped += 1
                    continue
        despiked.append(b)

    if dropped and log:
        log("{} {}: dropped {} corrupt bar(s) ({}->{} kept)".format(
            symbol, kind, dropped, len(bars), len(despiked)))
    return despiked


def _bar_date(bar):
    """Date portion of a bar's timestamp ('YYYY-MM-DD...' -> date) or None."""
    t = str(bar.get("t", ""))
    try:
        return datetime.strptime(t[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def aggregate_weekly(daily):
    """Roll up an ascending list of daily bars into ISO-week buckets.

    Databento has no native weekly schema, so weekly bars are aggregated from
    its clean daily series: open=first day, high=max, low=min, close=last day,
    volume=sum. Bars whose timestamp can't be parsed are skipped. Returns an
    ascending list of {o,h,l,c,v,t}.
    """
    weeks = {}
    order = []
    for b in daily:
        d = _bar_date(b)
        if d is None:
            continue
        iso = d.isocalendar()
        key = (iso[0], iso[1])
        w = weeks.get(key)
        if w is None:
            weeks[key] = {"o": b["o"], "h": b["h"], "l": b["l"],
                          "c": b["c"], "v": b.get("v", 0) or 0, "t": b["t"]}
            order.append(key)
        else:
            w["h"] = max(w["h"], b["h"])
            w["l"] = min(w["l"], b["l"])
            w["c"] = b["c"]
            w["v"] += b.get("v", 0) or 0
    return [weeks[k] for k in order]
