"""52-week ATM IV history backfill.

Without this, iv_rank.compute_iv_rank() returns garbage until the daily
snapshotter has been running for ~12 months. Two backfill paths:

  1. SPY / QQQ / IWM -> VIX / VXN / RVX history from yfinance.
     These indices ARE the 30-day implied vol of SPX / NDX / RUT
     (the underlying index of each ETF). Free, instant, perfect.

  2. Individual stocks -> Databento OPRA ohlcv-1d daily settlement
     prices for the closest-to-ATM 30-DTE contract pair on each day,
     fed through the Black-Scholes solver in vol_math.implied_vol.

Run from CLI:
    python iv_backfill.py --symbol SPY
    python iv_backfill.py --symbol AAPL --days 260
    python iv_backfill.py --all                 # full universe

The iv_history.db table has PRIMARY KEY (symbol, obs_date) so re-runs
are idempotent.
"""
import os
import sys
import argparse
import sqlite3
from datetime import datetime, date, timedelta

import requests
import pytz

import db_utils
import iv_rank
import vol_math


# Map index ETFs to their canonical IV index. These are exact matches
# (SPY tracks SPX, QQQ tracks NDX-100, IWM tracks RUT) so the index
# ATM 30-day IV is what an ATM 30-DTE option on the ETF would imply,
# minus a small basis for tracking error and dividend yield -- close
# enough for IV Rank purposes (we care about percentile, not absolute).
VIX_PROXY_MAP = {
    "SPY": "^VIX",
    "QQQ": "^VXN",
    "IWM": "^RVX",
}

# ATM contract selection window. Centered at 30 DTE, narrow enough that
# the chosen contract is consistently liquid, wide enough that we always
# find one even on weeks where no expiry sits exactly at 30 days.
DTE_TARGET = 30
DTE_MIN    = 21
DTE_MAX    = 45

RISK_FREE_RATE = 0.05        # ~3-month T-bill, close enough for IVR

# Cost guard for the Databento path. get_historical_daily_options pulls a
# full-year OPRA `definition` + `ohlcv-1d` stream per symbol -- the same
# "fat" query class whose unguarded use previously tripped the 402 billing
# breaker. metadata.get_cost is free, so every pull is priced first; any
# symbol over the per-symbol cap is skipped, and the run stops adding spend
# once the cumulative estimate hits the total cap. Skipped symbols simply
# retry on a later run (the caller re-attempts whatever has no history yet).
MAX_PER_SYMBOL_USD = float(os.getenv("IV_BACKFILL_MAX_PER_SYMBOL_USD", "2.0"))
MAX_TOTAL_USD      = float(os.getenv("IV_BACKFILL_MAX_TOTAL_USD", "25.0"))
_est_spend = 0.0   # cumulative estimated USD this process

# Why the most recent backfill_symbol call wrote 0 rows: None (it wrote
# rows), "budget", "estimate_failed", or "no_data". Mirrors the
# LAST_EARNINGS_ERROR pattern in safety_gates -- callers that orchestrate a
# universe run need to distinguish "skip now, retry later" (budget) from
# "this symbol has nothing to give" (no_data).
LAST_SKIP_REASON = None


def _estimate_pull_cost(symbol, start, end):
    """Estimated USD for the two OPRA pulls backfill_from_databento will
    issue, or None when the estimate itself fails (treat as: don't pull)."""
    import databento_adapter
    total = 0.0
    for schema in ("definition", "ohlcv-1d"):
        cost = databento_adapter.get_cost_estimate(
            "OPRA.PILLAR", [symbol + ".OPT"], schema,
            start.isoformat(), end.isoformat(), stype_in="parent")
        if not isinstance(cost, float):
            return None
        total += cost
    return total


# =============================================
# ALPACA DAILY BARS (for spot history)
# =============================================

ALPACA_KEY    = os.getenv("APCA_API_KEY_ID", "").strip()
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY", "").strip()
_ALPACA_HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}
_DATA_URL = "https://data.alpaca.markets/v2/stocks/{}/bars"


def _alpaca_daily_closes(symbol, start_date, end_date):
    """Returns dict {YYYY-MM-DD: close_price} across the date range."""
    if not ALPACA_KEY:
        return {}
    out = {}
    page_token = None
    for _ in range(20):  # cap pagination
        params = {
            "timeframe": "1Day",
            "start":     start_date.isoformat(),
            "end":       end_date.isoformat(),
            "limit":     1000,
            "feed":      "iex",
        }
        if page_token:
            params["page_token"] = page_token
        try:
            r = requests.get(_DATA_URL.format(symbol),
                             headers=_ALPACA_HEADERS,
                             params=params, timeout=15)
            if r.status_code != 200:
                break
            data = r.json()
        except Exception:
            break
        for bar in data.get("bars", []) or []:
            ts = bar.get("t", "")[:10]
            if ts:
                out[ts] = float(bar.get("c", 0.0))
        page_token = data.get("next_page_token")
        if not page_token:
            break
    return out


# =============================================
# PATH 1: VIX/VXN/RVX BACKFILL (free, instant)
# =============================================

def backfill_from_vix_proxy(symbol, days=252):
    """
    Pull yfinance history for the symbol's IV index proxy and write each
    daily close (divided by 100 to convert 18.5% -> 0.185 decimal) into
    iv_history. Returns rows_written.
    """
    proxy = VIX_PROXY_MAP.get(symbol)
    if not proxy:
        return 0

    try:
        import yfinance as yf
    except ImportError:
        print("[iv_backfill] yfinance not installed -- can't backfill {}".format(symbol))
        return 0

    end   = date.today()
    start = end - timedelta(days=int(days * 1.5) + 14)  # buffer for weekends

    try:
        df = yf.download(proxy, start=start.isoformat(), end=end.isoformat(),
                         progress=False, auto_adjust=False)
    except Exception as e:
        print("[iv_backfill] yfinance download failed for {}: {}".format(proxy, e))
        return 0

    if df is None or len(df) == 0:
        return 0

    # Find the close column (yfinance occasionally returns a MultiIndex
    # when given a list of tickers -- be defensive).
    close_col = "Close" if "Close" in df.columns else df.columns[0]

    conn = db_utils.connect(iv_rank.IV_CACHE_DB)
    c    = conn.cursor()
    written = 0
    now_iso = datetime.utcnow().isoformat()
    try:
        for idx, val in df[close_col].items():
            try:
                obs_date = idx.strftime("%Y-%m-%d")
                vix_close = float(val)
                if vix_close <= 0 or vix_close > 200:
                    continue
                c.execute("""
                    INSERT OR REPLACE INTO iv_history
                    (symbol, obs_date, atm_iv, updated_at)
                    VALUES (?, ?, ?, ?)
                """, (symbol, obs_date, vix_close / 100.0, now_iso))
                written += 1
            except Exception:
                continue
    finally:
        conn.commit()
        conn.close()

    return written


# =============================================
# PATH 2: DATABENTO + BLACK-SCHOLES BACKFILL
# =============================================

def _pick_atm_pair(rows_for_date, spot):
    """
    Given all eligible-DTE option records for one day, return the
    closest-to-ATM (call, put) pair sharing the same expiry. Returns
    (call_dict, put_dict) or (None, None) when no usable pair exists.
    """
    if not rows_for_date or spot is None or spot <= 0:
        return None, None

    # Group by expiry; require both a call and a put with same strike
    by_expiry = {}
    for r in rows_for_date:
        by_expiry.setdefault(r["expiry"], []).append(r)

    best = None  # (dte_distance_to_30, strike_distance_to_spot, call, put)
    for expiry, rows in by_expiry.items():
        calls = {r["strike"]: r for r in rows if r["type"] == "call"}
        puts  = {r["strike"]: r for r in rows if r["type"] == "put"}
        common_strikes = set(calls).intersection(puts)
        if not common_strikes:
            continue
        atm_strike = min(common_strikes, key=lambda k: abs(k - spot))
        call = calls[atm_strike]
        put  = puts[atm_strike]
        dte_dist    = abs(call["dte"] - DTE_TARGET)
        strike_dist = abs(atm_strike - spot) / spot
        key = (dte_dist, strike_dist)
        if best is None or key < best[0]:
            best = (key, call, put)

    if best is None:
        return None, None
    return best[1], best[2]


def backfill_from_databento(symbol, days=252):
    """
    Pull a year of OPRA ohlcv-1d for `symbol`, spot history from Alpaca,
    pick the ATM 30-DTE call+put per day, solve BS for IV on each leg,
    average call+put IV, and write to iv_history.

    Returns rows_written.
    """
    global _est_spend, LAST_SKIP_REASON
    try:
        import databento_adapter
    except ImportError:
        print("[iv_backfill] databento_adapter not present")
        LAST_SKIP_REASON = "no_data"
        return 0
    if not databento_adapter.is_available():
        print("[iv_backfill] DATABENTO_API_KEY not set -- skipping {}".format(symbol))
        LAST_SKIP_REASON = "no_data"
        return 0

    end   = date.today()
    start = end - timedelta(days=int(days * 1.5) + 14)

    # Spot history is free (Alpaca) -- check it before debiting the budget.
    spot_by_date = _alpaca_daily_closes(symbol, start, end)
    if not spot_by_date:
        print("[iv_backfill] no spot history for {}".format(symbol))
        LAST_SKIP_REASON = "no_data"
        return 0

    # Price the OPRA pull before paying for it (get_cost is free).
    est = _estimate_pull_cost(symbol, start, end)
    if est is None:
        print("[iv_backfill] {}: cost estimate failed -- skipping rather "
              "than risk a blind pull".format(symbol))
        LAST_SKIP_REASON = "estimate_failed"
        return 0
    if est > MAX_PER_SYMBOL_USD:
        print("[iv_backfill] {}: est ${:.2f} exceeds ${:.2f}/symbol cap -- "
              "skipping".format(symbol, est, MAX_PER_SYMBOL_USD))
        LAST_SKIP_REASON = "budget"
        return 0
    if _est_spend + est > MAX_TOTAL_USD:
        print("[iv_backfill] {}: est ${:.2f} would push run total past "
              "${:.2f} cap (spent ~${:.2f}) -- skipping".format(
                  symbol, est, MAX_TOTAL_USD, _est_spend))
        LAST_SKIP_REASON = "budget"
        return 0
    _est_spend += est
    print("[iv_backfill] {}: est ${:.4f} (run total ~${:.2f})".format(
        symbol, est, _est_spend))

    rows = databento_adapter.get_historical_daily_options(
        symbol, start, end, dte_min=DTE_MIN, dte_max=DTE_MAX,
    )
    if not rows:
        print("[iv_backfill] no OPRA history for {}".format(symbol))
        LAST_SKIP_REASON = "no_data"
        return 0

    by_date = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r)

    conn = db_utils.connect(iv_rank.IV_CACHE_DB)
    c    = conn.cursor()
    written = 0
    skipped = 0
    now_iso = datetime.utcnow().isoformat()

    try:
        for obs_date, day_rows in by_date.items():
            spot = spot_by_date.get(obs_date)
            if spot is None or spot <= 0:
                skipped += 1
                continue
            call, put = _pick_atm_pair(day_rows, spot)
            if not call or not put:
                skipped += 1
                continue

            T = call["dte"] / 365.0
            iv_call = vol_math.implied_vol(
                call["close"], spot, call["strike"], T,
                r=RISK_FREE_RATE, option_type="call",
            )
            iv_put = vol_math.implied_vol(
                put["close"], spot, put["strike"], T,
                r=RISK_FREE_RATE, option_type="put",
            )
            ivs = [v for v in (iv_call, iv_put) if v is not None]
            if not ivs:
                skipped += 1
                continue
            atm_iv = sum(ivs) / len(ivs)
            if atm_iv <= 0 or atm_iv > 5.0:
                skipped += 1
                continue

            c.execute("""
                INSERT OR REPLACE INTO iv_history
                (symbol, obs_date, atm_iv, updated_at)
                VALUES (?, ?, ?, ?)
            """, (symbol, obs_date, round(atm_iv, 4), now_iso))
            written += 1
    finally:
        conn.commit()
        conn.close()

    if skipped:
        print("[iv_backfill] {}: {} days written, {} skipped (no spot/no ATM/IV solver failed)"
              .format(symbol, written, skipped))
    return written


# =============================================
# DRIVER
# =============================================

def backfill_symbol(symbol, days=252):
    """
    Route a symbol to the right backfill path. Returns dict with stats;
    when 0 rows were written, `skip_reason` says why ("budget" /
    "estimate_failed" are retryable later; "no_data" is structural).
    """
    global LAST_SKIP_REASON
    LAST_SKIP_REASON = None
    if symbol in VIX_PROXY_MAP:
        n = backfill_from_vix_proxy(symbol, days=days)
        source = "vix_proxy({})".format(VIX_PROXY_MAP[symbol])
        if n == 0:
            LAST_SKIP_REASON = "no_data"
    else:
        n = backfill_from_databento(symbol, days=days)
        source = "databento+bs"
    out = {"symbol": symbol, "source": source, "rows": n}
    if n == 0 and LAST_SKIP_REASON:
        out["skip_reason"] = LAST_SKIP_REASON
    return out


def backfill_universe(symbols, days=252):
    results = []
    for s in symbols:
        try:
            results.append(backfill_symbol(s, days=days))
        except Exception as e:
            results.append({"symbol": s, "source": "error", "rows": 0,
                            "error": str(e)})
    return results


def _default_universe():
    """Mirrors main.SYMBOLS (the SPY/QQQ product set); computed lazily so we
    don't have to import all of main.py just to discover symbols."""
    try:
        import main
        return sorted(set(main.SYMBOLS))
    except Exception:
        return ["SPY", "QQQ"]


def main():
    p = argparse.ArgumentParser(description="Backfill 52-week ATM IV history.")
    p.add_argument("--symbol", help="Single symbol to backfill")
    p.add_argument("--days",   type=int, default=252,
                   help="Trading days of history (default 252 = 1y)")
    p.add_argument("--all",    action="store_true",
                   help="Backfill the full SYMBOLS + SWING_UNIVERSE set")
    args = p.parse_args()

    if args.symbol:
        symbols = [args.symbol.upper()]
    elif args.all:
        symbols = _default_universe()
    else:
        p.print_help()
        sys.exit(2)

    print("Backfilling IV history for {} symbol(s), {} days each..."
          .format(len(symbols), args.days))
    results = backfill_universe(symbols, days=args.days)
    print()
    for r in results:
        print("  {:<6} {:<22} -> {} rows".format(
            r["symbol"], r["source"], r["rows"]))
    total = sum(r["rows"] for r in results)
    print("\nTotal rows written: {}".format(total))


if __name__ == "__main__":
    main()
