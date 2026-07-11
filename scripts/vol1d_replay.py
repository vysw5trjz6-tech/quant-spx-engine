#!/usr/bin/env python3
"""
Replay the VIX1D proxy over historical sessions and report tracking error
vs the official Cboe VIX1D close (spec §7.1).

Data: Databento OPRA.PILLAR cbbo-1m (1-min consolidated BBO), restricted to
the two nearest SPXW expiries and strikes within a window around the prior
SPX close so a session costs cents, not dollars. EVERY pull is priced with
metadata.get_cost first and refused over the caps — same discipline as
iv_backfill. Official closes come from Cboe's free VIX1D history CSV.

Run on the deployment (needs DATABENTO_API_KEY + cdn.cboe.com egress):

    python scripts/vol1d_replay.py --days 5
    python scripts/vol1d_replay.py --date 2026-07-08 --date 2026-07-09
    python scripts/vol1d_replay.py --days 5 --dry-run     # cost estimate only

Report per session: snapshot count, proxy close (15:55), official close,
residual; then mean/max |residual| across sessions. Tune deltaK/forward/
weights against THIS before trusting the live signal.
"""

import argparse
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

import pytz

from vol1d import proxy, qa, replay
from vol1d import config as vol1d_config

# Hard spend caps (USD, Databento metered estimate). The strike/expiry
# restriction keeps a session ~1-2 orders of magnitude below these.
MAX_PER_SESSION_USD = 3.0
MAX_TOTAL_USD       = 10.0

# Strike window around the prior SPX close. +/-6% covers ~8 daily sigmas at
# a 20-vol tape — wider than the two-consecutive-no-bid stop ever reaches.
STRIKE_WINDOW_PCT = 6.0

ET = pytz.timezone("America/New_York")


def _client():
    import databento
    key = os.getenv("DATABENTO_API_KEY", "").strip()
    if not key:
        print("DATABENTO_API_KEY not set -- run this on the deployment.")
        sys.exit(2)
    return databento.Historical(key)


def _px(v):
    """Normalize a Databento price to dollars (same guard as
    databento_adapter._scale_dbn_price)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v > 1e6:
        v = v / 1e9
    if not (0 < v < 1e5):
        return None
    return v


def _recent_sessions(n):
    d = datetime.now(ET).date() - timedelta(days=1)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def _spx_close_before(session_date):
    """Prior-session SPX close from Cboe's free history CSV (centers the
    strike window). Falls back to the option-strike median if unavailable."""
    import requests
    try:
        r = requests.get("https://cdn.cboe.com/api/global/us_indices/"
                         "daily_prices/SPX_History.csv", timeout=15)
        if r.status_code != 200:
            return None
        hist = qa.CboeCsvOfficialSource.parse_csv(r.text)
        if not hist:
            return None
        prior = [d for d in hist if d < session_date.isoformat()]
        return hist[max(prior)] if prior else None
    except Exception:
        return None


def _load_session_meta(client, session_date):
    """SPXW definitions for the session -> {instrument_id: meta dict},
    restricted to the two nearest expiries and the strike window."""
    start = session_date.isoformat()
    end   = (session_date + timedelta(days=1)).isoformat()
    df = client.timeseries.get_range(
        dataset="OPRA.PILLAR", symbols=["SPXW.OPT"], stype_in="parent",
        schema="definition", start=start, end=end).to_df()
    if df is None or df.empty:
        return {}

    meta = {}
    for _, row in df.iterrows():
        try:
            iid    = int(row["instrument_id"])
            strike = float(row["strike_price"])
            if strike > 100000:              # 1e9 fixed point
                strike /= 1e9
            expiry = row["expiration"]
            expiry = expiry.date() if hasattr(expiry, "date") else \
                datetime.fromisoformat(str(expiry)[:10]).date()
            cls = str(row.get("instrument_class", "")).upper()
            typ = "call" if "C" in cls else "put" if "P" in cls else None
            if typ is None:
                continue
            meta[iid] = {"root": "SPXW", "expiry": expiry, "type": typ,
                         "strike": strike}
        except Exception:
            continue

    expiries = sorted({m["expiry"] for m in meta.values()
                       if m["expiry"] >= session_date})[:2]
    if len(expiries) < 2:
        return {}
    center = _spx_close_before(session_date)
    if center is None:
        strikes = sorted(m["strike"] for m in meta.values()
                         if m["expiry"] == expiries[0])
        center = strikes[len(strikes) // 2] if strikes else None
    if center is None:
        return {}
    lo = center * (1 - STRIKE_WINDOW_PCT / 100.0)
    hi = center * (1 + STRIKE_WINDOW_PCT / 100.0)
    return {iid: m for iid, m in meta.items()
            if m["expiry"] in expiries and lo <= m["strike"] <= hi}


def _pull_bbo(client, session_date, iids, dry_run, spent_total):
    """cbbo-1m rows for the session, chunked by instrument_id with a
    get_cost gate per chunk. Returns (rows, est_usd) or (None, est)."""
    start = session_date.isoformat() + "T13:30"   # 09:30 ET in UTC
    end   = session_date.isoformat() + "T20:05"   # 16:05 ET
    CHUNK = 2000
    rows, est_session = [], 0.0
    for i in range(0, len(iids), CHUNK):
        batch = [str(x) for x in iids[i:i + CHUNK]]
        kwargs = dict(dataset="OPRA.PILLAR", symbols=batch,
                      stype_in="instrument_id", schema="cbbo-1m",
                      start=start, end=end)
        try:
            est = float(client.metadata.get_cost(**kwargs))
        except Exception as e:
            print("  cost estimate failed ({}) -- skipping session".format(e))
            return None, est_session
        est_session += est
        if est_session > MAX_PER_SESSION_USD or spent_total + est_session > MAX_TOTAL_USD:
            print("  est ${:.2f} would breach caps (${}/session, ${} total) "
                  "-- skipping".format(est_session, MAX_PER_SESSION_USD,
                                       MAX_TOTAL_USD))
            return None, est_session
        if dry_run:
            continue
        df = client.timeseries.get_range(**kwargs).to_df()
        if df is None or df.empty:
            continue
        for ts, row in df.iterrows():
            bid = _px(row.get("bid_px_00", row.get("bid_px")))
            ask = _px(row.get("ask_px_00", row.get("ask_px")))
            ts_et = ts.tz_convert("America/New_York").tz_localize(None) \
                if getattr(ts, "tzinfo", None) else ts
            rows.append({
                "minute": ts_et.replace(second=0, microsecond=0).to_pydatetime()
                          if hasattr(ts_et, "to_pydatetime") else ts_et,
                "key":    int(row["instrument_id"]),
                "bid":    bid or 0.0,
                "ask":    ask or 0.0,
            })
    return rows, est_session


def replay_session(client, session_date, interval_min, dry_run, spent_total):
    print("== {} ==".format(session_date))
    meta = _load_session_meta(client, session_date)
    if not meta:
        print("  no usable SPXW definitions -- skipped")
        return None, 0.0
    print("  contracts in window: {}".format(len(meta)))

    rows, est = _pull_bbo(client, session_date, sorted(meta), dry_run, spent_total)
    print("  est cost: ${:.2f}".format(est))
    if dry_run or rows is None:
        return None, est
    if not rows:
        print("  no BBO rows returned")
        return None, est

    book = replay.forward_fill_book(rows)
    cfg  = vol1d_config.get_config()
    series = []
    for minute in replay.session_minutes(session_date,
                                         interval_min=interval_min):
        snap = replay.snapshot_at(book, meta, minute)
        if snap is None:
            continue
        out = proxy.compute_vix1d(snap, now_et=minute, cfg=cfg)
        if out:
            series.append((minute.isoformat(), out["vix1d"]))
    print("  snapshots computed: {}/{}".format(
        len(series), len(replay.session_minutes(session_date,
                                                interval_min=interval_min))))
    return series, est


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", action="append", default=[],
                    help="session YYYY-MM-DD (repeatable)")
    ap.add_argument("--days", type=int, default=0,
                    help="or: the N most recent weekday sessions")
    ap.add_argument("--interval-min", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true",
                    help="print cost estimates only, pull nothing")
    args = ap.parse_args()

    dates = [datetime.strptime(d, "%Y-%m-%d").date() for d in args.date]
    if args.days:
        dates += _recent_sessions(args.days)
    dates = sorted(set(dates))
    if not dates:
        ap.error("pass --date or --days")

    client = _client()
    official = qa.CboeCsvOfficialSource().get_history() or {}

    sessions, spent = [], 0.0
    for d in dates:
        series, est = replay_session(client, d, args.interval_min,
                                     args.dry_run, spent)
        spent += est
        if series is None:
            continue
        sessions.append({
            "date":           d.isoformat(),
            "series":         series,
            "proxy_close":    series[-1][1] if series else None,
            "official_close": official.get(d.isoformat()),
        })

    print("\ntotal est cost: ${:.2f}".format(spent))
    if args.dry_run:
        return 0

    report = replay.tracking_report(sessions)
    print("\n=== TRACKING REPORT ===")
    for row in report["sessions"]:
        print("  {date}  n={n_snapshots:<3} proxy_close={proxy_close} "
              "official={official_close} residual={residual}".format(**row))
    if report["n_compared"]:
        print("  mean residual: {}  mean|r|: {}  max|r|: {}".format(
            report["mean_residual"], report["mean_abs_residual"],
            report["max_abs_residual"]))
    else:
        print("  (no sessions with both proxy and official closes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
