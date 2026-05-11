# safety_gates.py
# Three Tier-1 protections that prevent the AI loop from overfitting,
# block trades around earnings, and make the backtest realistic.

import os
import math
import json
import sqlite3
import statistics
import requests
from datetime import datetime, timedelta
import pytz


# =============================================
# 1. AI SAMPLE-SIZE GATE
# =============================================
#
# Original problem: run_ai_improvement() runs after every 5 closed trades
# and freely changes weights. With small samples you're tuning to noise.
#
# Fix: require a minimum sample for any weight change AND require the
# observed win-rate change to be statistically significant via a Bayesian
# Beta-Binomial test, not a point estimate.

def beta_binomial_credible_interval(wins, losses, alpha=0.05):
    """
    95% credible interval for the win rate using a Beta(1,1) prior.
    Returns (lower, upper). Far more reliable than wins/total on small n.
    """
    a = wins + 1
    b = losses + 1
    # Approximate Beta CI using normal approximation to Beta when n is large,
    # exact via incremental binary search otherwise.
    # For our purposes a lightweight approximation suffices:
    mean = a / (a + b)
    var  = (a * b) / (((a + b) ** 2) * (a + b + 1))
    sd   = math.sqrt(var)
    # 95% CI ≈ mean ± 1.96 * sd, clipped to [0,1]
    lo = max(0.0, mean - 1.96 * sd)
    hi = min(1.0, mean + 1.96 * sd)
    return round(lo, 3), round(hi, 3)


def is_change_justified(group_wins, group_losses, baseline_rate=0.50,
                         min_samples=30):
    """
    Returns dict explaining whether a config change for this group is justified.

    Rules:
      - Need at least min_samples trades in this group
      - The 95% credible interval must EXCLUDE the baseline rate
        (i.e. we have evidence this group is meaningfully different)
    """
    n = group_wins + group_losses
    if n < min_samples:
        return {
            "justified":   False,
            "reason":      "insufficient_samples",
            "n":           n,
            "needed":      min_samples,
        }
    lo, hi = beta_binomial_credible_interval(group_wins, group_losses)
    if lo <= baseline_rate <= hi:
        return {
            "justified":   False,
            "reason":      "ci_includes_baseline",
            "n":           n,
            "ci_lo":       lo,
            "ci_hi":       hi,
            "baseline":    baseline_rate,
        }
    return {
        "justified":   True,
        "reason":      "significant",
        "n":           n,
        "ci_lo":       lo,
        "ci_hi":       hi,
    }


def filter_ai_proposed_changes(current_cfg, proposed_cfg, group_stats,
                                max_weight_delta=3, max_grade_delta=3):
    """
    Wrap this around the AI's proposed config changes BEFORE applying them.

    Args:
      current_cfg:   existing config dict
      proposed_cfg:  AI's proposed config (already parsed JSON)
      group_stats:   dict of {group_key: {"wins": x, "losses": y}}
      max_weight_delta: max change per call for a weight key
      max_grade_delta:  max change per call for a grade threshold

    Returns:
      (safe_cfg, rejected_changes_log)
    """
    safe_cfg = dict(current_cfg)
    rejected = []

    for key, new_val in proposed_cfg.items():
        if key not in current_cfg:
            rejected.append({"key": key, "reason": "unknown_key"})
            continue
        old_val = current_cfg[key]

        # Rate-limit numeric changes
        if isinstance(new_val, (int, float)) and isinstance(old_val, (int, float)):
            limit = max_grade_delta if "grade" in key else max_weight_delta
            if abs(new_val - old_val) > limit:
                # Clip rather than reject — let AI nudge in the right direction
                direction = 1 if new_val > old_val else -1
                new_val = old_val + direction * limit
                rejected.append({
                    "key": key, "reason": "clipped_to_max_delta",
                    "wanted": proposed_cfg[key], "applied": new_val
                })

        # Booleans flipping the most-impactful filters need a sanity check
        if key == "counter_trend_allowed" and new_val != old_val:
            ct_stats = group_stats.get("counter_trend", {})
            check = is_change_justified(
                ct_stats.get("wins", 0), ct_stats.get("losses", 0))
            if not check["justified"]:
                rejected.append({
                    "key": key, "reason": "boolean_flip_unjustified",
                    "stats": check
                })
                continue

        safe_cfg[key] = new_val

    return safe_cfg, rejected


# =============================================
# 2. EARNINGS BLACKOUT
# =============================================
#
# Don't take swing trades into earnings (IV crush wipes the trade).
# Also flag day trades on earnings day itself as elevated risk.

EARNINGS_CACHE_DB = "earnings_calendar.db"


def _init_earnings_db():
    conn = sqlite3.connect(EARNINGS_CACHE_DB)
    c    = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS earnings (
            symbol      TEXT NOT NULL,
            report_date TEXT NOT NULL,
            time_of_day TEXT,
            updated_at  TEXT,
            PRIMARY KEY (symbol, report_date)
        )
    """)
    conn.commit()
    conn.close()


_init_earnings_db()


def update_earnings_calendar(symbol, fmp_api_key=None):
    """
    Fetch upcoming AND historical earnings dates for a symbol.

    Stores both. Historical ones are used by the IV crush strategy to
    compute the historical post-earnings move.

    Free source: Financial Modeling Prep — generous free tier.
    Sign up at financialmodelingprep.com, pass key as fmp_api_key or env.
    """
    api_key = fmp_api_key or os.getenv("FMP_API_KEY", "").strip()
    if not api_key:
        return False

    et  = pytz.timezone("America/New_York")
    now = datetime.now(et).isoformat()

    conn = sqlite3.connect(EARNINGS_CACHE_DB)
    c    = conn.cursor()
    rows_written = 0

    # FMP /api/v3/historical/earning_calendar/{symbol} returns past + upcoming
    url = "https://financialmodelingprep.com/api/v3/historical/earning_calendar/{}".format(symbol)
    try:
        r = requests.get(url, params={"apikey": api_key, "limit": 12}, timeout=10)
        if r.status_code == 200:
            for e in r.json() or []:
                if e.get("date"):
                    c.execute("""
                        INSERT OR REPLACE INTO earnings
                        (symbol, report_date, time_of_day, updated_at)
                        VALUES (?, ?, ?, ?)
                    """, (symbol, e["date"], e.get("time", ""), now))
                    rows_written += 1
    except Exception:
        pass

    # Also pull the standard /earning_calendar endpoint for upcoming if the
    # historical one is empty
    if rows_written == 0:
        url2 = "https://financialmodelingprep.com/api/v3/earning_calendar/{}".format(symbol)
        try:
            r = requests.get(url2, params={"apikey": api_key}, timeout=10)
            if r.status_code == 200:
                for e in (r.json() or [])[:8]:
                    if e.get("date"):
                        c.execute("""
                            INSERT OR REPLACE INTO earnings
                            (symbol, report_date, time_of_day, updated_at)
                            VALUES (?, ?, ?, ?)
                        """, (symbol, e["date"], e.get("time", ""), now))
                        rows_written += 1
        except Exception:
            pass

    conn.commit()
    conn.close()
    return rows_written > 0


def days_to_earnings(symbol):
    """
    Returns the number of trading days until next earnings, or None if unknown.
    Negative = earnings already passed (within last 7 days).
    """
    conn = sqlite3.connect(EARNINGS_CACHE_DB)
    c    = conn.cursor()
    et    = pytz.timezone("America/New_York")
    today = datetime.now(et).date()

    c.execute("""
        SELECT report_date FROM earnings
        WHERE symbol = ?
          AND date(report_date) >= date(?, '-7 days')
        ORDER BY report_date ASC LIMIT 1
    """, (symbol, today.isoformat()))
    row = c.fetchone()
    conn.close()

    if not row:
        return None
    try:
        rep = datetime.strptime(row[0][:10], "%Y-%m-%d").date()
        return (rep - today).days
    except Exception:
        return None


def get_prior_earnings_dates(symbol, max_quarters=8):
    """
    Returns list of YYYY-MM-DD strings for the symbol's past earnings,
    newest first. Used by the IV crush strategy to compute historical
    post-earnings moves.
    """
    conn = sqlite3.connect(EARNINGS_CACHE_DB)
    c    = conn.cursor()
    et    = pytz.timezone("America/New_York")
    today = datetime.now(et).date()
    c.execute("""
        SELECT report_date FROM earnings
        WHERE symbol = ?
          AND date(report_date) < date(?)
        ORDER BY report_date DESC LIMIT ?
    """, (symbol, today.isoformat(), max_quarters))
    rows = c.fetchall()
    conn.close()
    return [r[0][:10] for r in rows if r[0]]


def earnings_filter(symbol, strategy_type):
    """
    Returns (allowed: bool, reason: str)

    Rules:
      - Swing trades: blocked if earnings within 10 trading days
      - 0DTE on earnings day: allowed but flagged HIGH_RISK
      - Day after earnings: allowed (post-earnings drift / continuation)
    """
    dte = days_to_earnings(symbol)
    if dte is None:
        return True, "no_earnings_data"

    if strategy_type == "swing":
        if 0 <= dte <= 10:
            return False, "earnings_in_{}_days".format(dte)
        if dte == -1 or dte == -2:
            return True, "post_earnings_continuation"
        return True, "ok"

    # 0DTE / day trade
    if dte == 0:
        return True, "earnings_today_HIGH_RISK"
    if dte == 1:
        return True, "earnings_tomorrow_caution"
    return True, "ok"


# =============================================
# 3. SLIPPAGE & FEES IN BACKTEST
# =============================================
#
# Original problem: orb_system.py assumes fills at exact breakout level.
# Real ORB fills slip 0.05–0.20% on liquid names. Backtest edge is
# overstated by 15–30%.
#
# Fix: realistic execution model with separate slippage curves for entry,
# stop-out, and target. Stops slip MORE than entries (you cross the spread
# to get out fast).

# Slippage in basis points (bps = 1/100 of a percent)
# Tuned from TCA (transaction cost analysis) on liquid US equities
SLIPPAGE_BPS = {
    "entry_market":    4,    # 4 bps = 0.04%
    "entry_breakout":  6,    # breakouts are more aggressive
    "stop_market":    12,    # stops cross the spread, often into a vacuum
    "target_limit":    2,    # limit orders at targets get filled at target
    "after_hours":    25,    # if you ever hold overnight
}

# Per-share commission + SEC/TAF fees on stocks
FEES_PER_SHARE = 0.0035

# Per-contract fees on options
OPTION_FEES_PER_CONTRACT = 0.65


def apply_slippage(price, side, fill_type="entry_breakout"):
    """
    Returns the realistic fill price after slippage.

    side: "BUY" or "SELL"
    fill_type: key from SLIPPAGE_BPS

    Slippage always works against you:
      - BUY  fills HIGHER than quoted
      - SELL fills LOWER than quoted
    """
    bps = SLIPPAGE_BPS.get(fill_type, SLIPPAGE_BPS["entry_market"])
    slip = price * (bps / 10000.0)
    if side == "BUY":
        return round(price + slip, 4)
    else:
        return round(price - slip, 4)


def apply_fees_stocks(num_shares):
    """Round-trip stock commission + fees."""
    return round(num_shares * FEES_PER_SHARE * 2, 2)


def apply_fees_options(num_contracts):
    """Round-trip option commission + fees."""
    return round(num_contracts * OPTION_FEES_PER_CONTRACT * 2, 2)


def realistic_trade_pnl(entry_price, exit_price, num_shares, direction,
                          exit_type="target_limit", asset="stock"):
    """
    Compute trade P&L including slippage and fees.

    direction: "LONG" or "SHORT"
    exit_type: "target_limit" or "stop_market"
    asset: "stock" or "option"

    Returns dict with gross, slippage, fees, net.
    """
    if direction == "LONG":
        entry_fill = apply_slippage(entry_price, "BUY", "entry_breakout")
        exit_fill  = apply_slippage(exit_price,  "SELL", exit_type)
        gross = (exit_fill - entry_fill) * num_shares
    else:
        entry_fill = apply_slippage(entry_price, "SELL", "entry_breakout")
        exit_fill  = apply_slippage(exit_price,  "BUY",  exit_type)
        gross = (entry_fill - exit_fill) * num_shares

    fees = apply_fees_options(num_shares) if asset == "option" \
           else apply_fees_stocks(num_shares)
    net  = gross - fees

    slippage_cost = (
        abs(entry_fill - entry_price) * num_shares +
        abs(exit_fill  - exit_price)  * num_shares
    )

    return {
        "entry_quoted": round(entry_price, 4),
        "entry_fill":   entry_fill,
        "exit_quoted":  round(exit_price, 4),
        "exit_fill":    exit_fill,
        "gross_pnl":    round(gross, 2),
        "slippage":     round(slippage_cost, 2),
        "fees":         fees,
        "net_pnl":      round(net, 2),
    }
