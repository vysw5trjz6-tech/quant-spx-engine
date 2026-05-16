#!/usr/bin/env python3
# databento_cost_audit.py
#
# Read-only Databento spend audit. Uses metadata.get_cost (free — it does
# NOT consume credit) to price the *exact* queries the live scheduler
# issues, then extrapolates to the full daily sweep so we can decide how
# to rein in spend before changing any behavior.
#
# Usage:
#   DATABENTO_API_KEY=... python databento_cost_audit.py
#   DATABENTO_API_KEY=... python databento_cost_audit.py --sample AAPL,NVDA,SPY
#
# Prints per-query and per-symbol cost, then the projected daily total
# for _refresh_oi_snapshots (full universe) + _refresh_gex_snapshots.

import os
import sys
import argparse
from datetime import datetime, timedelta, timezone

import databento as db


# Mirror main.py's universes (kept in sync manually; this is a one-off tool).
SYMBOLS = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "AMD", "META", "MSFT",
           "AMZN", "AVGO", "INTC", "LRCX", "CRWV", "GEV", "VRT", "ANET"]
# Full SWING_UNIVERSE size lives in main.py; we only need the count here.
SWING_UNIVERSE_SIZE = 72


def _utcdate():
    return datetime.now(timezone.utc).date()


def price(client, label, **kw):
    """Return (label, usd, error)."""
    try:
        usd = client.metadata.get_cost(**kw)
        return (label, float(usd), None)
    except Exception as e:
        return (label, None, "{}: {}".format(type(e).__name__, e))


def audit_symbol(client, sym):
    """Price the two OPRA queries get_options_chain_snapshot() issues."""
    parent = sym + ".OPT"
    end    = _utcdate()
    start  = end - timedelta(days=2)            # matches the adapter window
    rows = []
    for schema in ("definition", "statistics"):
        rows.append(price(
            client, "{}:{}".format(sym, schema),
            dataset="OPRA.PILLAR", symbols=[parent], stype_in="parent",
            schema=schema, start=start.isoformat(), end=end.isoformat(),
        ))
    return rows


def audit_backfill(client, sym):
    """Price the 'fat' 1-year IV-backfill queries (run rarely, but big)."""
    parent = sym + ".OPT"
    end    = _utcdate()
    start  = end - timedelta(days=365)
    rows = []
    for schema in ("definition", "ohlcv-1d"):
        rows.append(price(
            client, "{}:backfill:{}".format(sym, schema),
            dataset="OPRA.PILLAR", symbols=[parent], stype_in="parent",
            schema=schema, start=start.isoformat(), end=end.isoformat(),
        ))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", default="SPY,AAPL,NVDA",
                    help="comma-separated symbols to price individually")
    ap.add_argument("--backfill", action="store_true",
                    help="also price the 1-year IV-backfill queries")
    args = ap.parse_args()

    key = os.getenv("DATABENTO_API_KEY", "").strip()
    if not key:
        print("DATABENTO_API_KEY not set", file=sys.stderr)
        return 1
    client = db.Historical(key)

    sample = [s.strip().upper() for s in args.sample.split(",") if s.strip()]
    print("=== Per-query cost (OI/GEX snapshot path, 2-day OPRA window) ===")
    per_symbol = {}
    for sym in sample:
        rows = audit_symbol(client, sym)
        sym_total = 0.0
        for label, usd, err in rows:
            if err:
                print("  {:<28} ERROR {}".format(label, err))
            else:
                print("  {:<28} ${:.4f}".format(label, usd))
                sym_total += usd
        per_symbol[sym] = sym_total
        print("  {:<28} ${:.4f}  <- per-symbol snapshot".format(
            sym + ":TOTAL", sym_total))

    if per_symbol:
        avg = sum(per_symbol.values()) / len(per_symbol)
        print("\n=== Projected daily spend ===")
        print("  avg per-symbol snapshot:        ${:.4f}".format(avg))
        print("  _refresh_oi_snapshots ({} syms): ${:.2f}".format(
            SWING_UNIVERSE_SIZE, avg * SWING_UNIVERSE_SIZE))
        print("  _refresh_gex_snapshots (2 syms): ${:.2f}".format(avg * 2))
        print("  --- one full daily cycle:       ${:.2f}".format(
            avg * (SWING_UNIVERSE_SIZE + 2)))
        print("  NOTE: a redeploy/restart after 16:30 ET triggers another "
              "full cycle (cold cache).")

    if args.backfill:
        print("\n=== IV-backfill 1-year OPRA pull (per symbol, run rarely) ===")
        for sym in sample:
            for label, usd, err in audit_backfill(client, sym):
                if err:
                    print("  {:<32} ERROR {}".format(label, err))
                else:
                    print("  {:<32} ${:.4f}".format(label, usd))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
