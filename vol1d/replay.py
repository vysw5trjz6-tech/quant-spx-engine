# vol1d/replay.py
# Pure snapshot-assembly logic for the historical replay harness
# (scripts/vol1d_replay.py). Kept separate from the CLI so the forward-fill
# and snapshot construction are offline-testable without Databento.
#
# Input: minute-stamped BBO rows per contract. Output: chain-source-format
# snapshots at a chosen cadence, ready for vol1d.proxy.compute_vix1d.

from datetime import datetime, timedelta


def forward_fill_book(rows):
    """rows: [{"minute": datetime (ET, naive, floored to the minute),
               "key": hashable contract id,
               "bid": float, "ask": float}, ...]

    Returns {minute: {key: (bid, ask)}} where each minute's book carries
    the latest quote at-or-before it (forward-filled across gaps — a strike
    that didn't re-quote this minute still has its standing market)."""
    if not rows:
        return {}
    by_minute = {}
    for r in rows:
        by_minute.setdefault(r["minute"], {})[r["key"]] = (r["bid"], r["ask"])

    minutes = sorted(by_minute)
    book = {}
    standing = {}
    m = minutes[0]
    last = minutes[-1]
    while m <= last:
        standing.update(by_minute.get(m, {}))
        book[m] = dict(standing)
        m += timedelta(minutes=1)
    return book


def snapshot_at(book, meta, minute, spot=None):
    """Build a vol1d.chain_source-format snapshot from the forward-filled
    book at `minute`. `meta` maps key -> {"root","expiry","type","strike"}.
    Returns None when the book has nothing at/before that minute."""
    if minute not in book:
        past = [m for m in book if m <= minute]
        if not past:
            return None
        minute_key = max(past)
    else:
        minute_key = minute

    quotes = []
    for key, (bid, ask) in book[minute_key].items():
        m = meta.get(key)
        if not m:
            continue
        q = dict(m)
        q["bid"] = bid
        q["ask"] = ask
        quotes.append(q)
    if not quotes:
        return None
    return {"ts": minute, "spot": spot, "quotes": quotes, "source": "replay"}


def session_minutes(session_date, start_hm=(9, 35), end_hm=(15, 55),
                    interval_min=5):
    """Snapshot timestamps through one RTH session (naive ET datetimes)."""
    start = datetime(session_date.year, session_date.month, session_date.day,
                     *start_hm)
    end   = datetime(session_date.year, session_date.month, session_date.day,
                     *end_hm)
    out = []
    t = start
    while t <= end:
        out.append(t)
        t += timedelta(minutes=interval_min)
    return out


def tracking_report(sessions):
    """sessions: [{"date": iso, "proxy_close": float|None,
                   "official_close": float|None, "series": [(ts, level)]}]

    Returns summary stats: per-session residuals + mean/max absolute
    residual across sessions with both closes."""
    rows = []
    residuals = []
    for s in sessions:
        resid = None
        if s.get("proxy_close") is not None and s.get("official_close") is not None:
            resid = round(s["proxy_close"] - s["official_close"], 3)
            residuals.append(resid)
        rows.append({
            "date":           s["date"],
            "n_snapshots":    len(s.get("series") or []),
            "proxy_close":    s.get("proxy_close"),
            "official_close": s.get("official_close"),
            "residual":       resid,
        })
    summary = {"sessions": rows, "n_compared": len(residuals)}
    if residuals:
        abs_r = [abs(r) for r in residuals]
        summary["mean_residual"]     = round(sum(residuals) / len(residuals), 3)
        summary["mean_abs_residual"] = round(sum(abs_r) / len(abs_r), 3)
        summary["max_abs_residual"]  = round(max(abs_r), 3)
    return summary
