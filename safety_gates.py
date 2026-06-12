# safety_gates.py
# Three Tier-1 protections that prevent the AI loop from overfitting,
# block trades around earnings, and make the backtest realistic.

import os
import math
import json
import sqlite3
import db_utils
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


# Which trade group must be statistically significant before a given key
# may change. Anything unmapped falls back to the "global" pool.
_KEY_GROUP = {
    "grade_a_min":           "grade_a",
    "grade_b_min":           "grade_b",
    "grade_c_min":           "grade_c",
    "counter_trend_allowed": "counter_trend",
    "rank_align_bonus":      "counter_trend",
    "rank_align_penalty":    "counter_trend",
    "vwap_reclaim_enabled":  "vwap_reclaim",
    "weight_rs":             "rs",
}

# Max absolute distance a key may sit from its DEFAULT_CONFIG value, so
# week-over-week tuning cannot wander unboundedly even within delta limits.
def _drift_band(key):
    if key.startswith("weight_"):          return 10
    if key.startswith("grade_"):           return 10
    if key == "late_entry_hour":           return 2.0
    if key.startswith("rank_"):            return 15
    return None  # booleans / unmapped: no numeric band


def filter_ai_proposed_changes(current_cfg, proposed_cfg, group_stats,
                                default_cfg=None, min_samples=30,
                                max_weight_delta=3, max_grade_delta=3):
    """
    Wrap this around the AI's proposed config changes BEFORE applying them.

    A change is allowed ONLY if the trade group that justifies it has
    >= min_samples trades AND its win-rate credible interval excludes the
    0.50 baseline (i.e. there is real signal, not noise). Allowed numeric
    changes are then per-run delta-clipped and hard-bounded to within a
    drift band of default_cfg so they cannot run away over many weeks.

    Args:
      current_cfg:  existing config dict
      proposed_cfg: AI's proposed config (parsed JSON)
      group_stats:  {group_key: {"wins": x, "losses": y}}
      default_cfg:  baseline anchor (DEFAULT_CONFIG); drift bound is skipped
                    if not provided
      min_samples:  minimum trades in the driving group

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

        if new_val == old_val:
            continue  # no-op, nothing to gate

        # Every change must be justified by its driving group's sample.
        grp_name = _KEY_GROUP.get(key, "global")
        grp      = group_stats.get(grp_name, {})
        check    = is_change_justified(
            grp.get("wins", 0), grp.get("losses", 0),
            min_samples=min_samples)
        if not check["justified"]:
            rejected.append({
                "key": key, "reason": "unjustified_{}".format(check["reason"]),
                "group": grp_name, "stats": check,
            })
            continue

        # Numeric: per-run delta clip, then absolute drift bound vs baseline.
        if isinstance(new_val, (int, float)) and \
           isinstance(old_val, (int, float)) and \
           not isinstance(new_val, bool):
            limit = max_grade_delta if "grade" in key else max_weight_delta
            if abs(new_val - old_val) > limit:
                step    = limit if new_val > old_val else -limit
                new_val = old_val + step
                rejected.append({
                    "key": key, "reason": "clipped_to_max_delta",
                    "wanted": proposed_cfg[key], "applied": new_val,
                })
            band = _drift_band(key)
            if default_cfg is not None and band is not None \
               and key in default_cfg:
                base = default_cfg[key]
                lo, hi = base - band, base + band
                if new_val < lo or new_val > hi:
                    clamped = max(lo, min(hi, new_val))
                    rejected.append({
                        "key": key, "reason": "drift_bound_to_baseline",
                        "wanted": new_val, "applied": clamped,
                        "baseline": base,
                    })
                    new_val = clamped

        safe_cfg[key] = new_val

    return safe_cfg, rejected


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _renormalize_weights(vals, lo=5, hi=40, target=100):
    """
    Return integer weights, each within [lo, hi], summing exactly to
    target -- provided that is feasible (len*lo <= target <= len*hi).
    Residual is distributed only onto keys with headroom in the needed
    direction, so the band is never violated.
    """
    keys = list(vals)
    if not keys:
        return {}
    v = {k: _clamp(float(vals[k]), lo, hi) for k in keys}

    for _ in range(100):
        diff = target - sum(v.values())
        if abs(diff) < 1e-9:
            break
        movable = [k for k in keys
                   if (diff > 0 and v[k] < hi) or (diff < 0 and v[k] > lo)]
        if not movable:
            break
        share = diff / len(movable)
        for k in movable:
            v[k] = _clamp(v[k] + share, lo, hi)

    r = {k: int(round(v[k])) for k in keys}
    for _ in range(100):
        d = target - sum(r.values())
        if d == 0:
            break
        step = 1 if d > 0 else -1
        cand = [k for k in keys if lo <= r[k] + step <= hi]
        if not cand:
            break
        # Adjust whichever key the rounding most shortchanged.
        k = max(cand, key=lambda k: (v[k] - r[k]) * step)
        r[k] += step
    return r


def enforce_config_invariants(cfg):
    """
    Hard structural rules enforced in code (never trusted to the prompt):
      - grade floors + strict ordering  a_min > b_min > c_min
      - each weight_* in [5, 40], weights renormalized to sum 100
      - late_entry_hour within the trading day
    Mutates a copy and returns it.
    """
    c = dict(cfg)

    # Grade thresholds: floors + ordering.
    c["grade_c_min"] = int(_clamp(round(c.get("grade_c_min", 35)), 25, 85))
    c["grade_b_min"] = int(_clamp(round(c.get("grade_b_min", 55)),
                                  c["grade_c_min"] + 5, 90))
    c["grade_a_min"] = int(_clamp(round(c.get("grade_a_min", 75)),
                                  max(65, c["grade_b_min"] + 5), 95))

    # Factor weights: each in [5,40] AND sum to 100. Naive scale-then-round
    # can push a weight out of band, so water-fill the residual only onto
    # keys that still have headroom, then fix integer drift the same way.
    wkeys = [k for k in c if k.startswith("weight_")]
    if wkeys:
        c.update(_renormalize_weights(
            {k: c.get(k, 0) for k in wkeys}, lo=5, hi=40, target=100))

    if "late_entry_hour" in c:
        c["late_entry_hour"] = round(
            _clamp(float(c["late_entry_hour"]), 10.0, 15.5), 2)

    return c


# =============================================
# 2. EARNINGS BLACKOUT
# =============================================
#
# Don't take swing trades into earnings (IV crush wipes the trade).
# Also flag day trades on earnings day itself as elevated risk.

EARNINGS_CACHE_DB = db_utils.data_path("earnings_calendar.db")

# Last error seen while fetching the earnings calendar, so the daily refresh
# caller can report *why* a 0/N run happened instead of failing silently.
LAST_EARNINGS_ERROR = None


def _init_earnings_db():
    conn = db_utils.connect(EARNINGS_CACHE_DB)
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
    Fetch upcoming + historical earnings dates for a symbol via Yahoo Finance
    (yfinance). No API key required.

    Past dates are kept for the IV-crush strategy's historical move stats;
    upcoming dates feed the earnings_filter() block on swing trades.

    The fmp_api_key argument is accepted for backwards-compat but ignored.
    Returns True if any rows were written.
    """
    global LAST_EARNINGS_ERROR
    try:
        import yfinance as yf
    except ImportError:
        LAST_EARNINGS_ERROR = "yfinance not installed"
        return False

    et  = pytz.timezone("America/New_York")
    now = datetime.now(et).isoformat()

    try:
        ticker = yf.Ticker(symbol)
        df = ticker.earnings_dates
    except Exception as e:
        # Capture WHY so a 0/N refresh isn't a silent black box. A uniform
        # failure across every symbol is almost always Yahoo rate-limiting /
        # blocking the (datacenter) IP, not 72 individually-missing calendars.
        LAST_EARNINGS_ERROR = "{}: {}".format(type(e).__name__, str(e)[:200])
        return False

    if df is None or len(df) == 0:
        return False

    conn = db_utils.connect(EARNINGS_CACHE_DB)
    c    = conn.cursor()
    rows_written = 0

    try:
        for idx in df.index:
            try:
                report_date = idx.strftime("%Y-%m-%d")
            except Exception:
                continue
            # Yahoo's index time tells us BMO vs AMC: <10:00 ET = BMO,
            # >=14:00 ET = AMC. Close enough for the safety gate.
            try:
                hr = idx.hour
                tod = "amc" if hr >= 14 else ("bmo" if hr < 10 else "")
            except Exception:
                tod = ""
            c.execute("""
                INSERT OR REPLACE INTO earnings
                (symbol, report_date, time_of_day, updated_at)
                VALUES (?, ?, ?, ?)
            """, (symbol, report_date, tod, now))
            rows_written += 1
    finally:
        conn.commit()
        conn.close()

    return rows_written > 0


def days_to_earnings(symbol):
    """
    Returns the number of trading days until next earnings, or None if unknown.
    Negative = earnings already passed (within last 7 days).
    """
    conn = db_utils.connect(EARNINGS_CACHE_DB)
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
    conn = db_utils.connect(EARNINGS_CACHE_DB)
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
