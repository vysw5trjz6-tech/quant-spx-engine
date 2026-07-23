# overnight_context.py
# Overnight futures context — the missing edge layer.
#
# What this gives you:
#   1. ES/NQ/RTY overnight (Globex) high, low, range, midpoint
#   2. Overnight inventory model (Dalton): where did ES close vs where it
#      traded overnight? Long-liquidation, short-covering, or balanced?
#   3. Gap classification: where does today's RTH open sit relative to the
#      overnight range? Each class has a distinct base rate.
#
# Provider priority:
#   1. Databento (real CME futures — best)
#   2. Yahoo Finance continuous futures (ES=F/NQ=F — free, full Globex session)
#   3. Alpaca ETF extended-hours bars (SPY/QQQ/IWM) as the last-ditch proxy.
#      NOTE: the ETF proxy only prints during the 4:00-9:30 AM ET premarket,
#      so it structurally MISSES the actual overnight high/low. It exists only
#      so the brief degrades instead of vanishing when both futures sources
#      are down.

import os
import statistics
import requests
from datetime import datetime, timedelta, time as dtime
import pytz

ALPACA_KEY    = os.getenv("APCA_API_KEY_ID", "").strip()
ALPACA_SECRET = os.getenv("APCA_API_SECRET_KEY", "").strip()


# =============================================
# DATA FETCHERS
# =============================================

def _fetch_alpaca_extended_hours(symbol, start_iso, end_iso, feed="iex"):
    """
    Fallback for futures: use SPY/QQQ extended hours bars from Alpaca.
    Less ideal than real ES/NQ data but workable for retail.

    `feed` selects the Alpaca data feed: "iex" covers regular + pre/postmarket
    (4:00 AM-8:00 PM ET); "boats" is the Blue Ocean ATS overnight session
    (8:00 PM-4:00 AM ET). See _fetch_alpaca_overnight for how the two stitch
    into a full overnight session.
    """
    headers = {
        "APCA-API-KEY-ID":     ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }
    url = "https://data.alpaca.markets/v2/stocks/{}/bars".format(symbol)
    try:
        r = requests.get(url, headers=headers, params={
            "timeframe": "5Min",
            "start":     start_iso,
            "end":       end_iso,
            "limit":     10000,
            "feed":      feed,
        }, timeout=12)
        if r.status_code != 200:
            return []
        return r.json().get("bars", []) or []
    except Exception:
        return []


_ALPACA_PROXY_MAP = {"ES": "SPY", "NQ": "QQQ", "RTY": "IWM"}


def _fetch_alpaca_overnight(contract, start_iso, end_iso):
    """
    Full overnight session for `contract`'s ETF proxy (ES→SPY, NQ→QQQ,
    RTY→IWM), stitched from two Alpaca feeds:

      * IEX extended-hours bars — prev-day postmarket (4:00-8:00 PM ET) and
        today's premarket (4:00-9:30 AM ET), and
      * Blue Ocean ATS overnight bars (feed=boats, 8:00 PM-4:00 AM ET).

    The plain extended-hours proxy only prints during the 4:00-9:30 AM ET
    premarket, so it structurally MISSES the real overnight high/low; the
    boats leg fills that 8 PM-4 AM gap. Preferred over Yahoo because it rides
    Alpaca's authenticated API, which — unlike Yahoo's scrape endpoints —
    doesn't rate-limit datacenter (Railway) IPs.

    Free-plan boats data is 15-min delayed, so bars newer than ~15 min don't
    exist yet; we cap the request end 16 min back to both reflect that and
    avoid Alpaca's "end must be at least 15 minutes old" rejection on delayed
    overnight requests.

    Returns raw Alpaca bar dicts (t/o/h/l/c/v) ascending, deduped by
    timestamp, or [] on failure. Bars are in ETF dollars → source "etf_proxy".
    """
    proxy = _ALPACA_PROXY_MAP.get(contract, "SPY")
    if not (ALPACA_KEY and ALPACA_SECRET):
        return []

    try:
        start_dt = datetime.fromisoformat(start_iso)
        end_dt   = datetime.fromisoformat(end_iso)
    except (ValueError, TypeError):
        return []

    now = datetime.now(pytz.timezone("America/New_York"))
    safe_end = min(end_dt, now - timedelta(minutes=16))
    if safe_end <= start_dt:
        return []
    end_capped = safe_end.isoformat()

    iex   = _fetch_alpaca_extended_hours(proxy, start_iso, end_capped, feed="iex")
    boats = _fetch_alpaca_extended_hours(proxy, start_iso, end_capped, feed="boats")

    # Merge + dedupe by bar timestamp. The two feeds cover disjoint hours, so
    # overlaps are only at session boundaries; keeping either is equivalent.
    merged = {}
    for b in (iex or []) + (boats or []):
        t = b.get("t")
        if t:
            merged[t] = b
    return [merged[t] for t in sorted(merged)]


_YAHOO_FUTURES = {"ES": "ES=F", "NQ": "NQ=F", "RTY": "RTY=F", "YM": "YM=F"}

# Sanity bounds (futures points): reject mis-scaled or garbage prints before
# they poison the overnight range/inventory read. Same idea as
# index_data._LEVEL_BOUNDS.
_FUTURES_BOUNDS = {
    "ES":  (1500,  30000),
    "NQ":  (5000,  80000),
    "RTY": (800,   10000),
    "YM":  (10000, 100000),
}


def _fetch_yahoo_futures(contract, start_iso, end_iso):
    """
    Continuous front-month futures bars from Yahoo Finance (ES=F, NQ=F, ...).

    Free, key-less, and covers the FULL Globex session — unlike the SPY/QQQ
    extended-hours proxy, which only prints during the 4:00-9:30 AM ET
    premarket and structurally misses the real overnight high/low.

    Returns an ascending list of {t, o, h, l, c, v} dicts in futures points,
    clipped to [start_iso, end_iso), or [] on any failure (yfinance missing,
    Yahoo blocked on the host, empty frame).
    """
    ticker = _YAHOO_FUTURES.get(contract)
    if not ticker:
        return []
    try:
        import yfinance as yf
    except ImportError:
        return []

    lo, hi = _FUTURES_BOUNDS.get(contract, (0.0, float("inf")))
    try:
        start_dt = datetime.fromisoformat(start_iso)
        end_dt   = datetime.fromisoformat(end_iso)
        hist = yf.Ticker(ticker).history(
            start=start_dt, end=end_dt, interval="5m", auto_adjust=False)
        if hist is None or hist.empty:
            return []
        bars = []
        for ts, row in hist.iterrows():
            try:
                if ts.tzinfo is None:
                    continue  # can't place a naive stamp in the window safely
                if ts < start_dt or ts >= end_dt:
                    continue
                o = float(row["Open"])
                h = float(row["High"])
                l = float(row["Low"])
                c = float(row["Close"])
            except (KeyError, TypeError, ValueError):
                continue
            if h < l or not all(lo <= v <= hi for v in (o, h, l, c)):
                continue
            try:
                vol = int(row.get("Volume") or 0)
            except (TypeError, ValueError):
                vol = 0
            bars.append({
                "t": ts.isoformat(),
                "o": o, "h": h, "l": l, "c": c,
                "v": max(vol, 0),
            })
        return bars
    except Exception as e:
        print("[overnight] Yahoo {} futures fetch failed: {}".format(
            contract, e))
        return []


# =============================================
# OVERNIGHT SESSION DEFINITION
# =============================================
#
# US overnight session: 6:00 PM ET (Sunday) → 9:30 AM ET next morning.
# Globex actually opens 6 PM ET, which we use for ES/NQ/RTY.
#
# For weekday overnights: 4:15 PM ET previous day → 9:30 AM ET today.

def overnight_window(target_date_et):
    """
    Returns (start_iso_utc, end_iso_utc) for the overnight session leading
    into target_date_et (which is a date object in US/Eastern).
    """
    et = pytz.timezone("America/New_York")

    end = et.localize(datetime.combine(target_date_et, dtime(9, 30)))
    # Start of overnight = previous trading day's RTH close + 45 min (4:15 PM ET)
    prev = target_date_et - timedelta(days=1)
    while prev.weekday() >= 5:  # skip weekends
        prev -= timedelta(days=1)
    start = et.localize(datetime.combine(prev, dtime(16, 15)))

    return start.isoformat(), end.isoformat()


def _get_overnight_bars(target_date_et, contract="ES"):
    """
    Get overnight bars. Provider priority:
      1. Databento (real CME futures — best, but only when the account can
         read the full session; it defers premarket without a live license)
      2. Alpaca overnight — IEX extended-hours + Blue Ocean ATS (feed=boats),
         the full session in ETF dollars, served over an authenticated API
         that (unlike Yahoo) isn't rate-limited from datacenter/Railway IPs
      3. Yahoo Finance continuous futures (ES=F/NQ=F — real futures points,
         full Globex, but flaky from cloud hosts)
      4. Alpaca IEX extended-hours only (premarket coverage — last-ditch)
    Returns (bars, source) where bars is a list of {t, o, h, l, c, v} dicts
    and source is 'futures' or 'etf_proxy'. The source matters downstream:
    futures bars are in futures points, proxy bars in ETF dollars, and the
    two must never be compared against each other's levels.
    """
    # --- Path 1: Databento real futures ---
    try:
        import databento_adapter
        if databento_adapter.is_available():
            bars = databento_adapter.get_overnight_bars(contract, target_date_et)
            if bars:
                bars = [b for b in bars if all(b.get(k) is not None
                                                 for k in ("o","h","l","c"))]
                if bars:
                    return bars, "futures"
    except ImportError:
        pass
    except Exception:
        pass

    start_iso, end_iso = overnight_window(target_date_et)

    # --- Path 2: Alpaca overnight (IEX extended + Blue Ocean ATS) ---
    bars = _fetch_alpaca_overnight(contract, start_iso, end_iso)
    if bars:
        return bars, "etf_proxy"

    # --- Path 3: Yahoo continuous futures (real overnight, futures points) ---
    bars = _fetch_yahoo_futures(contract, start_iso, end_iso)
    if bars:
        return bars, "futures"

    # --- Path 4: Alpaca IEX extended-hours proxy (premarket only, last-ditch) ---
    proxy = _ALPACA_PROXY_MAP.get(contract, "SPY")
    return _fetch_alpaca_extended_hours(proxy, start_iso, end_iso), "etf_proxy"


# =============================================
# OVERNIGHT METRICS
# =============================================

def overnight_range(target_date_et=None, contract="ES"):
    """
    Returns dict:
      {
        high, low, range, mid,
        first_print, last_print,
        upper_third_volume, middle_third_volume, lower_third_volume,
        close_location_in_range  (0.0 = bottom, 1.0 = top)
      }
    """
    if target_date_et is None:
        et = pytz.timezone("America/New_York")
        target_date_et = datetime.now(et).date()

    bars, source = _get_overnight_bars(target_date_et, contract)
    if not bars:
        return None

    highs = [b["h"] for b in bars]
    lows  = [b["l"] for b in bars]
    high  = max(highs)
    low   = min(lows)
    rng   = high - low
    if rng <= 0:
        return None

    mid = (high + low) / 2.0
    first = bars[0]["o"]
    last  = bars[-1]["c"]

    # Volume distribution by thirds of the range (key for inventory model)
    upper_thresh = high - rng / 3.0
    lower_thresh = low + rng / 3.0
    upper_v = middle_v = lower_v = 0.0
    for b in bars:
        typ = (b["h"] + b["l"] + b["c"]) / 3.0
        v   = b.get("v", 0)
        if typ >= upper_thresh:
            upper_v += v
        elif typ <= lower_thresh:
            lower_v += v
        else:
            middle_v += v

    close_loc = (last - low) / rng

    return {
        "high":           round(high, 2),
        "low":            round(low, 2),
        "range":          round(rng, 2),
        "mid":            round(mid, 2),
        "first_print":    round(first, 2),
        "last_print":     round(last, 2),
        "close_loc":      round(close_loc, 2),
        "upper_v":        upper_v,
        "middle_v":       middle_v,
        "lower_v":        lower_v,
        "bar_count":      len(bars),
        "source":         source,
    }


# =============================================
# OVERNIGHT INVENTORY MODEL (Dalton)
# =============================================
#
# Premise: where price spent overnight tells you what positions traders
# are stuck holding into RTH open. If ES rallied to new highs but closed
# in lower third = "long liquidation" coming = fade the open.
#
# Categories:
#   LONG_LIQUIDATION   : closed lower third after upper-third trade
#   SHORT_COVERING     : closed upper third after lower-third trade
#   BALANCED           : middle third dominant, no commitment
#   ONE_TIMEFRAME_UP   : made highs at close, no rotation
#   ONE_TIMEFRAME_DOWN : made lows at close, no rotation

def overnight_inventory(on_data, prev_rth_close):
    """
    Classify overnight inventory.

    Args:
      on_data: output of overnight_range()
      prev_rth_close: yesterday's 4 PM ET close price

    Returns dict with category, signal_bias, conviction (0-1).
    """
    if not on_data or prev_rth_close is None:
        return None

    high = on_data["high"]
    low  = on_data["low"]
    last = on_data["last_print"]
    rng  = on_data["range"]
    upper_v  = on_data["upper_v"]
    middle_v = on_data["middle_v"]
    lower_v  = on_data["lower_v"]
    total_v  = upper_v + middle_v + lower_v

    if total_v <= 0 or rng <= 0:
        return None

    # Where did volume concentrate?
    upper_pct  = upper_v  / total_v
    middle_pct = middle_v / total_v
    lower_pct  = lower_v  / total_v

    # Where did it close in the range?
    close_loc = on_data["close_loc"]  # 0..1

    # Was high or low explored relative to prev close?
    explored_up   = high > prev_rth_close + rng * 0.5
    explored_down = low  < prev_rth_close - rng * 0.5

    category = "BALANCED"
    bias     = "NEUTRAL"
    conviction = 0.3

    if explored_up and close_loc < 0.33 and upper_pct > 0.30:
        category   = "LONG_LIQUIDATION"
        bias       = "BEAR"  # fade the open or expect early weakness
        conviction = 0.7
    elif explored_down and close_loc > 0.66 and lower_pct > 0.30:
        category   = "SHORT_COVERING"
        bias       = "BULL"
        conviction = 0.7
    elif close_loc > 0.85 and middle_pct < 0.4:
        category   = "ONE_TIMEFRAME_UP"
        bias       = "BULL"
        conviction = 0.65
    elif close_loc < 0.15 and middle_pct < 0.4:
        category   = "ONE_TIMEFRAME_DOWN"
        bias       = "BEAR"
        conviction = 0.65
    elif middle_pct > 0.55:
        category   = "BALANCED"
        bias       = "NEUTRAL"
        conviction = 0.4

    return {
        "category":   category,
        "bias":       bias,
        "conviction": conviction,
        "close_loc":  close_loc,
        "upper_pct":  round(upper_pct, 2),
        "middle_pct": round(middle_pct, 2),
        "lower_pct":  round(lower_pct, 2),
    }


# =============================================
# GAP CLASSIFICATION RELATIVE TO OVERNIGHT
# =============================================
#
# Standard gap analysis is "vs yesterday's close". Better: classify by where
# today's open sits relative to the OVERNIGHT range. Three categories:
#
#   INSIDE_GAP    : open within ON range → likely fills, fade the open
#   OUTSIDE_GAP   : open beyond ON high/low → momentum continuation likely
#   GAP_AND_GO    : open extends beyond ON range AND prev RTH range → strong

def classify_gap(rth_open, prev_rth_close, on_data, prev_rth_high=None,
                  prev_rth_low=None):
    """
    Returns dict with classification and base-rate-based bias.
    """
    if not on_data or rth_open is None or prev_rth_close is None:
        return None

    on_high = on_data["high"]
    on_low  = on_data["low"]
    gap_pct = (rth_open - prev_rth_close) / prev_rth_close * 100

    direction = "UP" if gap_pct > 0 else "DOWN"

    classification = None
    note = ""

    if on_low <= rth_open <= on_high:
        classification = "INSIDE_GAP"
        note = "Open inside overnight range → 70% fill rate, fade the open"

    elif rth_open > on_high:
        # Gap above overnight high
        if prev_rth_high is not None and rth_open > prev_rth_high * 1.005:
            classification = "GAP_AND_GO_UP"
            note = "Open above ON high AND prev RTH high → strong continuation"
        else:
            classification = "OUTSIDE_GAP_UP"
            note = "Open above ON high → momentum likely, watch ON high as support"

    elif rth_open < on_low:
        if prev_rth_low is not None and rth_open < prev_rth_low * 0.995:
            classification = "GAP_AND_GO_DOWN"
            note = "Open below ON low AND prev RTH low → strong continuation"
        else:
            classification = "OUTSIDE_GAP_DOWN"
            note = "Open below ON low → momentum likely, watch ON low as resistance"
    else:
        classification = "UNKNOWN"

    # Base-rate biases (from CME group / Hedgeye empirical work)
    base_rates = {
        "INSIDE_GAP":       {"fade_win_rate": 0.68, "trend_win_rate": 0.32},
        "OUTSIDE_GAP_UP":   {"fade_win_rate": 0.45, "trend_win_rate": 0.55},
        "OUTSIDE_GAP_DOWN": {"fade_win_rate": 0.45, "trend_win_rate": 0.55},
        "GAP_AND_GO_UP":    {"fade_win_rate": 0.30, "trend_win_rate": 0.70},
        "GAP_AND_GO_DOWN":  {"fade_win_rate": 0.30, "trend_win_rate": 0.70},
    }

    return {
        "class":       classification,
        "direction":   direction,
        "gap_pct":     round(gap_pct, 2),
        "rth_open":    round(rth_open, 2),
        "on_high":     on_high,
        "on_low":      on_low,
        "note":        note,
        "base_rates":  base_rates.get(classification, {}),
    }


# =============================================
# REAL-INDEX PREMARKET CONTEXT (SPX / NDX points)
# =============================================
#
# The overnight bars are futures points (or ETF dollars on the fallback
# path); the levels a trader acts on are cash-index points. The bridge is a
# ratio anchor: the overnight series' first print lands at ~4:00-4:15 PM ET,
# i.e. right at the prior RTH close in the SAME instrument's units, so
#
#     index_level ≈ real_prev_index_close × (series_level / first_print)
#
# The units cancel, which makes this correct for both the ES-futures and the
# SPY-proxy path, and it folds the futures/cash basis out automatically.

def index_premarket_context(on_data, prev_bar, rth_open=None,
                             proxy_prev_close=None):
    """
    Convert an overnight_range() read into real index points.

    prev_bar: the index's prior completed session {date, o, h, l, c}
              (index_data.prev_session output).
    rth_open / proxy_prev_close: optional — today's RTH open and prior close
              of the SAME proxy instrument (e.g. SPY). When both are given
              (post-open refresh), a full open-vs-overnight-range gap
              classification is included under "gap".

    Returns dict in index points, or None when inputs are unusable:
      prev_close/high/low, implied_open, gap_pct, premarket_class,
      on_high, on_low, gap (classify_gap output, post-open only).
    """
    if not on_data or not prev_bar:
        return None
    first = on_data.get("first_print")
    prev_close = prev_bar.get("c")
    if not first or first <= 0 or not prev_close:
        return None

    scale        = float(prev_close) / float(first)
    implied_open = on_data["last_print"] * scale
    on_high      = on_data["high"] * scale
    on_low       = on_data["low"]  * scale
    gap_pct      = (implied_open - prev_close) / prev_close * 100.0

    prev_high = prev_bar.get("h")
    prev_low  = prev_bar.get("l")

    # Premarket classification: where the implied open sits vs YESTERDAY'S
    # range. (Classification vs the overnight range is meaningless before
    # the bell — the implied open is by construction inside it.)
    if prev_high and implied_open > prev_high:
        prem_class = "ABOVE_PREV_HIGH"
        prem_note  = "implied open above yesterday's high — continuation watch"
    elif prev_low and implied_open < prev_low:
        prem_class = "BELOW_PREV_LOW"
        prem_note  = "implied open below yesterday's low — continuation watch"
    elif abs(gap_pct) < 0.10:
        prem_class = "FLAT"
        prem_note  = "flat open implied — no gap edge"
    else:
        prem_class = "INSIDE_PREV_RANGE"
        prem_note  = "implied open inside yesterday's range — fill/fade likely"

    out = {
        "prev_date":       prev_bar.get("date"),
        "prev_close":      round(float(prev_close), 2),
        "prev_high":       round(float(prev_high), 2) if prev_high else None,
        "prev_low":        round(float(prev_low), 2) if prev_low else None,
        "implied_open":    round(implied_open, 2),
        "gap_pct":         round(gap_pct, 2),
        "on_high":         round(on_high, 2),
        "on_low":          round(on_low, 2),
        "premarket_class": prem_class,
        "premarket_note":  prem_note,
        "source":          prev_bar.get("source"),
        "gap":             None,
    }

    # Post-open: convert the actual RTH open into index points via the same
    # proxy ratio, then run the full open-vs-overnight classification.
    if rth_open and proxy_prev_close:
        rth_open_idx = float(prev_close) * (float(rth_open) /
                                             float(proxy_prev_close))
        out["rth_open"] = round(rth_open_idx, 2)
        out["gap"] = classify_gap(rth_open_idx, prev_close,
                                   {"high": on_high, "low": on_low},
                                   prev_high, prev_low)
    return out


# =============================================
# UNIFIED PRE-MARKET BRIEF
# =============================================
#
# Single function that wraps everything for the dashboard top bar.

def get_premarket_brief(prev_rth_close, prev_rth_high=None, prev_rth_low=None,
                          rth_open=None, spx_prev=None, ndx_prev=None):
    """
    Top-bar dashboard data: ES/NQ overnight range + inventory, real-index
    (SPX/NDX) premarket context, gap class. Call once at 9:10-9:25 AM ET.

    prev_rth_close/high/low and rth_open are in the SPY proxy's units (what
    main.py has on hand); they are only compared against overnight bars on
    the ETF-proxy fallback path where the units actually match.
    spx_prev / ndx_prev: real index prior-session bars from
    index_data.prev_session(), enabling index-point context.
    """
    et   = pytz.timezone("America/New_York")
    today = datetime.now(et).date()

    on_es = overnight_range(today, "ES")
    on_nq = overnight_range(today, "NQ")

    # Inventory anchors on the series' own first print (~the prior RTH close
    # in the same units). Anchoring on the SPY close while the bars were ES
    # futures made explored_up/down always-true garbage.
    inv_es = overnight_inventory(on_es, on_es["first_print"]) if on_es else None
    inv_nq = overnight_inventory(on_nq, on_nq["first_print"]) if on_nq else None

    spx_ctx = index_premarket_context(on_es, spx_prev, rth_open,
                                       prev_rth_close) if on_es else None
    ndx_ctx = index_premarket_context(on_nq, ndx_prev) if on_nq else None

    # Legacy top-level gap (consumed by plan_summary and opening-drive):
    # prefer the unit-safe index-point classification; fall back to the raw
    # proxy comparison ONLY when the bars are actually the same instrument.
    gap = None
    if spx_ctx and spx_ctx.get("gap"):
        gap = spx_ctx["gap"]
    elif (rth_open is not None and on_es is not None
            and on_es.get("source") == "etf_proxy"):
        gap = classify_gap(rth_open, prev_rth_close, on_es,
                            prev_rth_high, prev_rth_low)

    return {
        "es_overnight":  on_es,
        "nq_overnight":  on_nq,
        "es_inventory":  inv_es,
        "nq_inventory":  inv_nq,
        "spx":           spx_ctx,
        "ndx":           ndx_ctx,
        "gap":           gap,
    }
