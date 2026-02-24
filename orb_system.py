# orb_system.py
# Institutional-grade ORB backtest engine
# Data: Alpaca Markets REST API (same source as live engine)
# Run locally: python orb_system.py

import requests
import os
import statistics
import csv
from datetime import datetime, date, timedelta
from collections import defaultdict

# =============================================
# CONFIGURATION
# =============================================

ALPACA_KEY    = os.getenv("APCA_API_KEY_ID", "").strip()
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY", "").strip()

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET
}

BARS_URL = "https://data.alpaca.markets/v2/stocks/{}/bars"

# Backtest window
START_DATE = "2024-01-01"
END_DATE   = "2024-12-31"

# Walk-forward split (70% in-sample, 30% out-of-sample)
INSAMPLE_RATIO = 0.70

# Symbols to backtest
SYMBOLS = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA"]

# ORB definition: 30 min = 6 x 5-min bars (institutional standard)
ORB_BARS = 6

# Strategy parameters
RISK_MULTIPLIER      = 2.0    # profit target in R
RISK_PERCENT         = 0.01   # 1% account risk per trade
ACCOUNT_SIZE         = 30000
VOL_CONFIRM_MULT     = 1.5    # breakout bar volume must be > 1.5x recent avg
GAP_FILTER_PCT       = 0.015  # skip days with opening gap > 1.5%
LATE_ENTRY_CUTOFF    = "11:30"  # no new entries after this ET time
MAX_DAILY_LOSS_R     = -3.0   # stop trading a symbol after -3R in one day

# Correlated pairs: if first symbol triggers, skip second
CORRELATION_PAIRS = [("SPY", "QQQ")]

# Output files
TRADE_LOG_FILE   = "trade_log.csv"
EQUITY_LOG_FILE  = "equity_curve.csv"


# =============================================
# DATA FETCHING
# =============================================

def fetch_bars(symbol, start, end, timeframe="5Min"):
    """
    Fetch historical bars from Alpaca with pagination.
    Returns list of bar dicts sorted by timestamp.
    """
    bars   = []
    params = {
        "start":     start + "T09:00:00Z",
        "end":       end   + "T23:59:00Z",
        "timeframe": timeframe,
        "limit":     10000,
        "feed":      "iex"
    }

    while True:
        try:
            r = requests.get(BARS_URL.format(symbol), headers=HEADERS,
                             params=params, timeout=15)
            if r.status_code != 200:
                print("  ERROR fetching {}: HTTP {} {}".format(
                    symbol, r.status_code, r.text[:150]))
                break

            data       = r.json()
            page_bars  = data.get("bars", [])
            bars.extend(page_bars)

            next_token = data.get("next_page_token")
            if not next_token:
                break
            params["page_token"] = next_token

        except Exception as e:
            print("  Exception fetching {}: {}".format(symbol, e))
            break

    print("  Fetched {} bars for {} ({} to {})".format(
        len(bars), symbol, start, end))
    return bars


def group_by_date(bars):
    """Group 5-min bars by trading date."""
    grouped = defaultdict(list)
    for b in bars:
        # timestamp format: 2024-01-02T09:30:00Z
        day = b["t"][:10]
        grouped[day].append(b)
    # Sort each day's bars by time
    for day in grouped:
        grouped[day].sort(key=lambda x: x["t"])
    return grouped


# =============================================
# FILTERS
# =============================================

def gap_filter(day_bars, prev_close):
    """
    Return True (allow trade) if opening gap is within threshold.
    Large gaps indicate news events where ORB fails.
    """
    if not prev_close or not day_bars:
        return False
    open_price = day_bars[0]["o"]
    gap_pct    = abs(open_price - prev_close) / prev_close
    return gap_pct <= GAP_FILTER_PCT


def volume_confirmation(breakout_bar, recent_bars):
    """
    Return True if breakout bar volume exceeds 1.5x average of prior bars.
    Confirms institutional participation.
    """
    if len(recent_bars) < 3:
        return True
    avg_vol = statistics.mean(b["v"] for b in recent_bars[-5:])
    return breakout_bar["v"] >= avg_vol * VOL_CONFIRM_MULT


def bar_time_et(bar_timestamp):
    """Extract HH:MM from bar timestamp (bars are in UTC, market hours offset)."""
    # Alpaca timestamps are UTC; ET is UTC-5 (EST) or UTC-4 (EDT)
    # For simplicity parse the hour/min and subtract 4 or 5
    # A production system would use pytz; here we use string parsing
    # Alpaca returns local timestamps when feed=iex so we use as-is
    return bar_timestamp[11:16]


def late_entry_filter(bar):
    """Return True (allow) if bar is before the cutoff time."""
    bar_time = bar_time_et(bar["t"])
    return bar_time <= LATE_ENTRY_CUTOFF


# =============================================
# ATR-BASED POSITION SIZING
# =============================================

def calculate_atr(daily_bars, period=14):
    """Calculate Average True Range over last N days."""
    if len(daily_bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(daily_bars)):
        high  = daily_bars[i]["h"]
        low   = daily_bars[i]["l"]
        prev  = daily_bars[i-1]["c"]
        tr    = max(high - low, abs(high - prev), abs(low - prev))
        trs.append(tr)
    return statistics.mean(trs[-period:])


def position_size_atr(account_size, risk_percent, atr):
    """Size position so 1 ATR move = risk_percent of account."""
    if not atr or atr <= 0:
        return 0
    risk_amount = account_size * risk_percent
    return round(risk_amount / atr, 2)


# =============================================
# CORE ORB BACKTEST ENGINE
# =============================================

def backtest_symbol(symbol, all_bars_5min, all_bars_daily):
    """
    Run full ORB backtest for one symbol.
    Returns list of trade result dicts.
    """
    grouped    = group_by_date(all_bars_5min)
    dates      = sorted(grouped.keys())
    trades     = []
    prev_close = None
    daily_r    = defaultdict(float)

    # Build daily close lookup for gap filter and ATR
    daily_closes = {}
    for db in all_bars_daily:
        day = db["t"][:10]
        daily_closes[day] = db["c"]

    daily_list = sorted(all_bars_daily, key=lambda x: x["t"])

    for date_str in dates:
        day_bars = grouped[date_str]

        if len(day_bars) < ORB_BARS + 2:
            prev_close = daily_closes.get(date_str, prev_close)
            continue

        # Gap filter
        if not gap_filter(day_bars, prev_close):
            prev_close = daily_closes.get(date_str, prev_close)
            continue

        # ATR-based sizing: use daily bars up to this date
        past_daily = [b for b in daily_list if b["t"][:10] < date_str]
        atr        = calculate_atr(past_daily) if len(past_daily) >= 15 else None
        size       = position_size_atr(ACCOUNT_SIZE, RISK_PERCENT, atr) if atr else 100

        # Define ORB using first 30 min
        orb        = day_bars[:ORB_BARS]
        orb_high   = max(b["h"] for b in orb)
        orb_low    = min(b["l"] for b in orb)
        range_size = orb_high - orb_low

        if range_size <= 0:
            prev_close = daily_closes.get(date_str, prev_close)
            continue

        position = None
        entry = stop = target = 0

        for i in range(ORB_BARS, len(day_bars)):
            bar = day_bars[i]

            # Daily loss limit per symbol
            if daily_r[date_str] <= MAX_DAILY_LOSS_R:
                break

            # Time filter: no late entries
            if position is None and not late_entry_filter(bar):
                break

            # Entry: long breakout
            if position is None and bar["h"] > orb_high:
                if volume_confirmation(bar, day_bars[max(0, i-5):i]):
                    position = "LONG"
                    entry    = orb_high
                    stop     = orb_low
                    target   = entry + (range_size * RISK_MULTIPLIER)

            # Entry: short breakout
            elif position is None and bar["l"] < orb_low:
                if volume_confirmation(bar, day_bars[max(0, i-5):i]):
                    position = "SHORT"
                    entry    = orb_low
                    stop     = orb_high
                    target   = entry - (range_size * RISK_MULTIPLIER)

            # Exit logic
            if position == "LONG":
                if bar["l"] <= stop:
                    r_mult = -1.0
                    trades.append(_trade_record(
                        symbol, date_str, position, entry, stop, target,
                        r_mult, size, atr))
                    daily_r[date_str] += r_mult
                    position = None
                    break
                if bar["h"] >= target:
                    r_mult = RISK_MULTIPLIER
                    trades.append(_trade_record(
                        symbol, date_str, position, entry, stop, target,
                        r_mult, size, atr))
                    daily_r[date_str] += r_mult
                    position = None
                    break

            if position == "SHORT":
                if bar["h"] >= stop:
                    r_mult = -1.0
                    trades.append(_trade_record(
                        symbol, date_str, position, entry, stop, target,
                        r_mult, size, atr))
                    daily_r[date_str] += r_mult
                    position = None
                    break
                if bar["l"] <= target:
                    r_mult = RISK_MULTIPLIER
                    trades.append(_trade_record(
                        symbol, date_str, position, entry, stop, target,
                        r_mult, size, atr))
                    daily_r[date_str] += r_mult
                    position = None
                    break

        prev_close = daily_closes.get(date_str, prev_close)

    return trades


def _trade_record(symbol, date_str, direction, entry, stop, target,
                  r_mult, size, atr):
    return {
        "symbol":    symbol,
        "date":      date_str,
        "direction": direction,
        "entry":     round(entry, 2),
        "stop":      round(stop, 2),
        "target":    round(target, 2),
        "r_mult":    round(r_mult, 2),
        "size":      size,
        "atr":       round(atr, 4) if atr else None,
        "outcome":   "WIN" if r_mult > 0 else "LOSS"
    }


# =============================================
# STATISTICS ENGINE
# =============================================

def compute_stats(trades, label="ALL"):
    if not trades:
        print("  No trades in {} sample".format(label))
        return {}

    rs        = [t["r_mult"] for t in trades]
    wins      = [r for r in rs if r > 0]
    losses    = [r for r in rs if r <= 0]
    win_rate  = len(wins) / len(rs)

    equity    = []
    running   = 0
    for r in rs:
        running += r
        equity.append(running)

    import math
    peak     = 0
    max_dd   = 0
    for e in equity:
        if e > peak:
            peak = e
        dd = e - peak
        if dd < max_dd:
            max_dd = dd

    avg_win  = statistics.mean(wins)  if wins   else 0
    avg_loss = statistics.mean(losses) if losses else 0

    gross_win  = sum(wins)
    gross_loss = abs(sum(losses)) if losses else 1
    profit_factor = gross_win / gross_loss if gross_loss else float("inf")

    # Sharpe ratio (annualised, assuming ~252 trading days)
    if len(rs) > 1:
        avg_r  = statistics.mean(rs)
        std_r  = statistics.stdev(rs)
        sharpe = (avg_r / std_r) * math.sqrt(252) if std_r > 0 else 0
    else:
        sharpe = 0

    # Max consecutive losses
    max_consec = cur_consec = 0
    for r in rs:
        if r < 0:
            cur_consec += 1
            max_consec  = max(max_consec, cur_consec)
        else:
            cur_consec  = 0

    return {
        "label":         label,
        "trades":        len(rs),
        "win_rate":      round(win_rate * 100, 1),
        "avg_r":         round(statistics.mean(rs), 3),
        "total_r":       round(sum(rs), 2),
        "max_drawdown":  round(max_dd, 2),
        "profit_factor": round(profit_factor, 2),
        "sharpe":        round(sharpe, 2),
        "avg_win":       round(avg_win, 3),
        "avg_loss":      round(avg_loss, 3),
        "max_consec_loss": max_consec,
        "equity_curve":  equity
    }


def monthly_breakdown(trades):
    """Return dict of {YYYY-MM: {trades, wins, total_r}}"""
    months = defaultdict(lambda: {"trades": 0, "wins": 0, "total_r": 0.0})
    for t in trades:
        mo = t["date"][:7]
        months[mo]["trades"]  += 1
        months[mo]["total_r"] += t["r_mult"]
        if t["r_mult"] > 0:
            months[mo]["wins"] += 1
    return dict(sorted(months.items()))


# =============================================
# OUTPUT / REPORTING
# =============================================

def print_stats(stats):
    print("")
    print("=" * 50)
    print("  RESULTS: {}".format(stats["label"]))
    print("=" * 50)
    print("  Trades:            {}".format(stats["trades"]))
    print("  Win Rate:          {}%".format(stats["win_rate"]))
    print("  Avg R per trade:   {}".format(stats["avg_r"]))
    print("  Total R:           {}".format(stats["total_r"]))
    print("  Max Drawdown (R):  {}".format(stats["max_drawdown"]))
    print("  Profit Factor:     {}".format(stats["profit_factor"]))
    print("  Sharpe Ratio:      {}".format(stats["sharpe"]))
    print("  Avg Win (R):       {}".format(stats["avg_win"]))
    print("  Avg Loss (R):      {}".format(stats["avg_loss"]))
    print("  Max Consec Losses: {}".format(stats["max_consec_loss"]))
    print("=" * 50)


def print_monthly(trades):
    breakdown = monthly_breakdown(trades)
    print("")
    print("  MONTHLY BREAKDOWN")
    print("  {:<10} {:>8} {:>8} {:>10}".format("Month", "Trades", "WinRate", "Total R"))
    print("  " + "-" * 40)
    for mo, d in breakdown.items():
        wr = round(d["wins"] / d["trades"] * 100, 1) if d["trades"] else 0
        print("  {:<10} {:>8} {:>7}% {:>10}".format(
            mo, d["trades"], wr, round(d["total_r"], 2)))


def save_trade_log(trades, filename=TRADE_LOG_FILE):
    if not trades:
        return
    fields = ["symbol", "date", "direction", "entry", "stop",
              "target", "r_mult", "size", "atr", "outcome"]
    with open(filename, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in trades:
            w.writerow({k: t.get(k, "") for k in fields})
    print("  Trade log saved: {}".format(filename))


def save_equity_curve(stats_list, filename=EQUITY_LOG_FILE):
    """Save combined equity curve from all symbols."""
    max_len = max(len(s.get("equity_curve", [])) for s in stats_list)
    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        headers = ["trade_num"] + [s["label"] for s in stats_list]
        writer.writerow(headers)
        for i in range(max_len):
            row = [i + 1]
            for s in stats_list:
                ec = s.get("equity_curve", [])
                row.append(ec[i] if i < len(ec) else ec[-1] if ec else 0)
            writer.writerow(row)
    print("  Equity curve saved: {}".format(filename))


# =============================================
# CORRELATION FILTER  (applied across symbols)
# =============================================

def apply_correlation_filter(all_trades):
    """
    If correlated symbols both triggered on the same day,
    keep only the higher-scoring one (by abs r_mult as proxy).
    """
    filtered = []
    by_date  = defaultdict(list)
    for t in all_trades:
        by_date[t["date"]].append(t)

    for day, day_trades in by_date.items():
        symbols_today = [t["symbol"] for t in day_trades]
        skip = set()
        for s1, s2 in CORRELATION_PAIRS:
            if s1 in symbols_today and s2 in symbols_today:
                # Keep whichever had the better outcome; drop the other
                t1 = next(t for t in day_trades if t["symbol"] == s1)
                t2 = next(t for t in day_trades if t["symbol"] == s2)
                drop = s2 if abs(t1["r_mult"]) >= abs(t2["r_mult"]) else s1
                skip.add((day, drop))

        for t in day_trades:
            if (t["date"], t["symbol"]) not in skip:
                filtered.append(t)

    return filtered


# =============================================
# WALK-FORWARD SPLIT
# =============================================

def walk_forward_split(trades):
    if not trades:
        return [], []
    sorted_trades = sorted(trades, key=lambda x: x["date"])
    split         = int(len(sorted_trades) * INSAMPLE_RATIO)
    return sorted_trades[:split], sorted_trades[split:]


# =============================================
# MAIN
# =============================================

def main():
    if not ALPACA_KEY or not ALPACA_SECRET:
        print("ERROR: Set APCA_API_KEY_ID and APCA_API_SECRET_KEY env vars before running")
        return

    print("")
    print("=" * 50)
    print("  INSTITUTIONAL ORB BACKTEST ENGINE")
    print("  Period: {} to {}".format(START_DATE, END_DATE))
    print("  Symbols: {}".format(", ".join(SYMBOLS)))
    print("  ORB window: {} bars ({}min)".format(ORB_BARS, ORB_BARS * 5))
    print("=" * 50)

    all_trades = []

    for symbol in SYMBOLS:
        print("")
        print("Fetching {}...".format(symbol))
        bars_5min = fetch_bars(symbol, START_DATE, END_DATE, timeframe="5Min")
        bars_daily = fetch_bars(symbol, START_DATE, END_DATE, timeframe="1Day")

        if not bars_5min or not bars_daily:
            print("  Skipping {} - no data".format(symbol))
            continue

        trades = backtest_symbol(symbol, bars_5min, bars_daily)
        print("  {} trades generated for {}".format(len(trades), symbol))
        all_trades.extend(trades)

    if not all_trades:
        print("No trades generated. Check API keys and date range.")
        return

    # Correlation filter
    all_trades = apply_correlation_filter(all_trades)
    all_trades.sort(key=lambda x: x["date"])

    # Walk-forward split
    insample, outsample = walk_forward_split(all_trades)

    # Compute stats
    stats_all  = compute_stats(all_trades,  label="FULL PERIOD")
    stats_in   = compute_stats(insample,    label="IN-SAMPLE (70%)")
    stats_out  = compute_stats(outsample,   label="OUT-OF-SAMPLE (30%)")

    # Print results
    print_stats(stats_all)
    print_monthly(all_trades)
    print_stats(stats_in)
    print_stats(stats_out)

    # Overfitting warning
    if stats_out.get("win_rate", 0) < stats_in.get("win_rate", 0) - 10:
        print("")
        print("  WARNING: Out-of-sample win rate dropped >10pts vs in-sample.")
        print("  Strategy may be overfit. Review parameters before live trading.")

    # Save outputs
    print("")
    save_trade_log(all_trades)
    save_equity_curve([stats_in, stats_out], filename=EQUITY_LOG_FILE)
    print("")
    print("Done.")


if __name__ == "__main__":
    main()
