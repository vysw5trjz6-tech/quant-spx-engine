#!/usr/bin/env python3
"""
Probe CBOE's official daily index CSVs for VIX / VIX9D / VIX3M.

Run this on the deployment that actually serves the regime filter (e.g.
Railway) BEFORE wiring CBOE in as a data source. It answers the only two
questions that matter:

  1. Is the endpoint reachable from this host's egress? (datacenter IPs often
     get blocked by Yahoo/Stooq -- the failure mode regime_filter.py already
     fights. CBOE's CDN usually is not blocked, but confirm here.)
  2. Is the data actually fresh -- i.e. does it carry the most recent trading
     day's close, not a stale mirror?

Exit code 0 only if all three legs are reachable AND parse to a recent date.

    python scripts/probe_cboe_vix.py
"""

import csv
import io
import sys
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("requests not installed; run inside the project venv")
    sys.exit(2)

CBOE = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{}_History.csv"
LEGS = ["VIX", "VIX9D", "VIX3M"]

# A close older than this many days means the feed is stale (covers a
# 3-day holiday weekend with margin).
MAX_STALE_DAYS = 5


def _parse_last_row(text):
    """Return (last_date:datetime, last_close:float, n_rows:int) or raise."""
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise ValueError("empty CSV")
    last = rows[-1]
    # CBOE header is: DATE,OPEN,HIGH,LOW,CLOSE  (DATE like MM/DD/YYYY)
    date_key  = next(k for k in last if k.strip().upper() == "DATE")
    close_key = next(k for k in last if k.strip().upper() == "CLOSE")
    d = datetime.strptime(last[date_key].strip(), "%m/%d/%Y")
    return d, float(last[close_key]), len(rows)


def probe(leg):
    url = CBOE.format(leg)
    try:
        r = requests.get(url, timeout=15)
    except Exception as e:
        return False, "{}: REQUEST FAILED: {}".format(leg, e)

    if r.status_code != 200 or not r.text:
        return False, "{}: HTTP {} (len={})".format(leg, r.status_code, len(r.text or ""))

    try:
        d, close, n = _parse_last_row(r.text)
    except Exception as e:
        return False, "{}: HTTP 200 but parse failed: {}".format(leg, e)

    age = (datetime.now(timezone.utc).replace(tzinfo=None) - d).days
    fresh = age <= MAX_STALE_DAYS
    status = "FRESH" if fresh else "STALE"
    msg = "{}: HTTP 200  rows={}  last={}  close={:.2f}  age={}d  [{}]".format(
        leg, n, d.strftime("%Y-%m-%d"), close, age, status)
    return fresh, msg


def main():
    print("Probing CBOE daily index CSVs ...\n")
    all_ok = True
    for leg in LEGS:
        ok, msg = probe(leg)
        all_ok = all_ok and ok
        print(("  OK  " if ok else "  XX  ") + msg)
    print()
    if all_ok:
        print("PASS - all three legs reachable and fresh. Safe to wire in.")
        return 0
    print("FAIL - at least one leg unreachable or stale. Do NOT rely on CBOE here.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
