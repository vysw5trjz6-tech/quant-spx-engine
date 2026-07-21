#!/usr/bin/env python3
"""
Probe CBOE's free delayed-quotes SPX/SPXW option chain (the vol1d proxy's
live data source) from the deployment host.

Mirrors scripts/probe_cboe_vix.py: run this on the box that actually serves
the engine (Railway) BEFORE trusting the vol1d module there. It answers:

  1. Is cdn.cboe.com/delayed_quotes reachable from this host's egress?
  2. Does the payload carry what the proxy needs — spot, SPXW roots, both
     of the two nearest expiries, and live bid/ask (not all zeros)?
  3. How stale is it? (Quotes are ~15-min delayed by design; flag anything
     materially worse.)

Exit code 0 only if the chain parses and a VIX1D proxy value computes.

    python scripts/probe_cboe_options.py
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

import pytz

from vol1d import proxy
from vol1d.chain_source import CboeDelayedChainSource


def main():
    print("Probing CBOE delayed-quotes SPX chain ...\n")
    src = CboeDelayedChainSource()
    snap = src.get_snapshot()
    if snap is None:
        print("  XX  fetch/parse failed -- see error above. Do NOT rely on "
              "this source here.")
        return 1

    expiries = sorted({q["expiry"] for q in snap["quotes"]})
    n_bid = sum(1 for q in snap["quotes"] if q["bid"] > 0)
    print("  OK  spot={:.2f}  quotes={}  bid>0={}  expiries={} (first 3: {})".format(
        snap["spot"], len(snap["quotes"]), n_bid, len(expiries),
        ", ".join(e.isoformat() for e in expiries[:3])))

    if n_bid == 0:
        print("  XX  every quote is no-bid (closed session or stale file)")
        return 1

    # gex_intraday (v2) needs per-option volume for the flow layer and the
    # chain's own timestamp for staleness. Both degrade gracefully when
    # absent (baseline-only GEX, fetch-time staleness) — but verify here
    # before trusting the flow layer on this host.
    n_vol = sum(1 for q in snap["quotes"] if q.get("volume"))
    n_oi = sum(1 for q in snap["quotes"] if q.get("open_interest"))
    ts_src = snap.get("chain_ts_source")
    print("  {}  gex_intraday fields: volume>0 on {}/{} quotes, oi>0 on {}, "
          "chain_ts={} (source={})".format(
              "OK" if (n_vol and ts_src == "payload") else "!!",
              n_vol, len(snap["quotes"]), n_oi,
              snap.get("chain_ts"), ts_src))
    if not n_vol:
        print("      volume absent/zero -> flow layer contributes nothing "
              "(baseline-only GEX). Fine off-hours; investigate during RTH.")
    if ts_src != "payload":
        print("      payload carries no parseable timestamp -> staleness "
              "falls back to fetch time.")

    now_et = datetime.now(pytz.timezone("America/New_York")).replace(tzinfo=None)
    out = proxy.compute_vix1d(snap, now_et=now_et)
    if out is None:
        print("  XX  chain fetched but proxy would not compute "
              "(likely outside RTH -- retry during the session)")
        return 1

    print("  OK  VIX1D proxy = {:.2f}  (near {} w1={:.2f} | next {} w2={:.2f})".format(
        out["vix1d"], out["near_expiry"], out["w1"],
        out["next_expiry"], out["w2"]))
    print("\nPASS - source usable for the vol1d module on this host.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
