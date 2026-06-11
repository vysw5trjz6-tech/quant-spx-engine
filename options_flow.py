# options_flow.py
# Opening-window options flow analysis.
#
# Window: 8:00 AM – 10:00 AM ET (2 hours)
#   - 8:00–9:30 captures pre-bell positioning / hedging
#   - 9:30–10:00 captures opening-drive flow
#   - 10:00 cutoff: edge decays sharply, noise dominates
#
# Symbols: SPY + QQQ only (full options flow data is heavy — restrict
# to the two indexes that drive everything else).
#
# Cost: SPY+QQQ trades for 2 hours ≈ 50-100 MB compressed.
# At ~$5-15/GB → roughly $0.50-1.50/day. With aggressive caching,
# usually $10-30/month.
#
# What it tells us:
#   - Was the open call-aggressed or put-aggressed?
#   - Net premium imbalance — institutions usually spend MORE on the side
#     they want; retail spreads thin.
#   - Block sweeps — single prints >$500K signal urgent positioning
#   - Repeated-strike accumulation — same strike hit many times = conviction

import os
import sqlite3
import db_utils
import json
from datetime import datetime, timedelta, time as dtime
import pytz

FLOW_DB = "options_flow.db"


def _init_db():
    conn = db_utils.connect(FLOW_DB)
    c    = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS flow_summaries (
            symbol      TEXT NOT NULL,
            flow_date   TEXT NOT NULL,

            -- Premium spent on each side (notional $ value)
            call_premium    REAL,
            put_premium     REAL,
            call_volume     INTEGER,
            put_volume      INTEGER,

            -- Aggressor breakdown
            call_buy_premium  REAL,
            call_sell_premium REAL,
            put_buy_premium   REAL,
            put_sell_premium  REAL,

            -- Block / sweep counts
            call_blocks   INTEGER,
            put_blocks    INTEGER,

            -- Top traded strikes (JSON)
            top_strikes   TEXT,

            stored_at   TEXT,
            PRIMARY KEY (symbol, flow_date)
        )
    """)
    conn.commit()
    conn.close()


_init_db()


# =============================================
# PULL OPRA TRADES IN THE WINDOW
# =============================================

def _dataset_available_end(client):
    """End of OPRA.PILLAR's available historical range as an aware UTC
    datetime, or None when the metadata call fails (callers then proceed
    and let the cost estimate surface any range error)."""
    try:
        rng = client.metadata.get_dataset_range("OPRA.PILLAR")
        raw = rng.get("end") if isinstance(rng, dict) else getattr(rng, "end", None)
        if raw is None:
            return None
        if isinstance(raw, datetime):
            dt = raw
        else:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        return dt
    except Exception:
        return None


def pull_opening_flow(symbol, target_date_et=None):
    """
    Pull options trades in 8:00 AM – 10:00 AM ET window for symbol.
    Stores summary metrics; returns dict with aggregates.

    Symbol = "SPY" or "QQQ" only (cost guard).
    """
    if symbol not in ("SPY", "QQQ"):
        return None

    try:
        import databento_adapter
    except ImportError:
        return None

    if not databento_adapter.is_available():
        return None

    et = pytz.timezone("America/New_York")
    if target_date_et is None:
        target_date_et = datetime.now(et).date()

    flow_date_str = target_date_et.isoformat()

    # Skip if already pulled today (cost-conscious)
    existing = load_flow(symbol, flow_date_str)
    if existing and existing.get("call_premium") is not None:
        return existing

    # Window: 8:00–10:00 ET = 12:00–14:00 UTC (EDT) or 13:00–15:00 UTC (EST)
    # Use ISO local format; Databento will interpret correctly
    window_start = target_date_et.isoformat() + "T08:00:00-04:00"
    window_end   = target_date_et.isoformat() + "T10:00:00-04:00"

    parent = symbol + ".OPT"

    client = databento_adapter._get_client()
    if not client:
        return None

    # OPRA historical availability lags real time (often by hours intraday).
    # Querying past the available range 422s and aborts the pull for the day;
    # check first and defer instead -- the scheduler retries until it lands.
    avail_end = _dataset_available_end(client)
    if avail_end is not None:
        try:
            window_end_dt = datetime.fromisoformat(window_end)
            if avail_end < window_end_dt:
                print("[flow] {} deferred: OPRA available to {} < window end {}"
                      .format(symbol, avail_end.isoformat(), window_end))
                return None
        except Exception:
            pass

    # Cost check first — abort if estimate is way over expected
    try:
        cost = client.metadata.get_cost(
            dataset  = "OPRA.PILLAR",
            symbols  = [parent],
            stype_in = "parent",
            schema   = "trades",
            start    = window_start,
            end      = window_end,
        )
        if cost > 5.0:
            print("[flow] {} aborting: cost ${:.2f} exceeds $5 guardrail".format(
                symbol, cost))
            return None
        print("[flow] {} window cost estimate: ${:.4f}".format(symbol, cost))
    except Exception as e:
        # If cost estimate fails, abort rather than risk surprise bill
        print("[flow] {} cost estimate failed: {} — aborting".format(symbol, e))
        return None

    # Pull definitions first (we need strike + type per instrument_id)
    try:
        def_df = client.timeseries.get_range(
            dataset  = "OPRA.PILLAR",
            symbols  = [parent],
            stype_in = "parent",
            schema   = "definition",
            start    = (target_date_et - timedelta(days=2)).isoformat(),
            end      = target_date_et.isoformat(),
        ).to_df()
    except Exception as e:
        print("[flow] {} definitions fetch failed: {}".format(symbol, e))
        return None

    if def_df is None or def_df.empty:
        return None

    # Build instrument_id -> {strike, type} map
    inst_map = {}
    for _, row in def_df.iterrows():
        try:
            iid = int(row.get("instrument_id"))
            strike = float(row.get("strike_price", 0))
            if strike > 100000:
                strike = strike / 1e9
            if strike <= 0:
                continue
            klass = str(row.get("instrument_class", "")).upper()
            opt_type = "call" if "C" in klass else "put" if "P" in klass else None
            if opt_type:
                inst_map[iid] = {"strike": strike, "type": opt_type}
        except Exception:
            continue

    # Pull trades + cmbp-1 quotes in window
    # cmbp-1 = consolidated NBBO; gives us bid/ask at trade time for
    # aggressor classification
    try:
        trades_df = client.timeseries.get_range(
            dataset  = "OPRA.PILLAR",
            symbols  = [parent],
            stype_in = "parent",
            schema   = "trades",
            start    = window_start,
            end      = window_end,
        ).to_df()
    except Exception as e:
        print("[flow] {} trades fetch failed: {}".format(symbol, e))
        return None

    if trades_df is None or trades_df.empty:
        return None

    # Try to get quotes for aggressor classification — optional, skip if costly
    try:
        cost_q = client.metadata.get_cost(
            dataset  = "OPRA.PILLAR",
            symbols  = [parent],
            stype_in = "parent",
            schema   = "tcbbo",   # trade + consolidated BBO
            start    = window_start,
            end      = window_end,
        )
        if cost_q < 3.0:
            quotes_df = client.timeseries.get_range(
                dataset  = "OPRA.PILLAR",
                symbols  = [parent],
                stype_in = "parent",
                schema   = "tcbbo",
                start    = window_start,
                end      = window_end,
            ).to_df()
        else:
            quotes_df = None
            print("[flow] {} skipping quotes — cost ${:.2f}".format(symbol, cost_q))
    except Exception:
        quotes_df = None

    return _summarize_trades(symbol, flow_date_str, trades_df, quotes_df, inst_map)


def _summarize_trades(symbol, flow_date_str, trades_df, quotes_df, inst_map):
    """Aggregate raw trades into the flow summary."""
    call_premium = 0.0
    put_premium  = 0.0
    call_volume  = 0
    put_volume   = 0

    call_buy_prem  = 0.0
    call_sell_prem = 0.0
    put_buy_prem   = 0.0
    put_sell_prem  = 0.0

    call_blocks = 0
    put_blocks  = 0

    strike_premium = {}  # (strike, type) -> total premium

    BLOCK_THRESHOLD_USD = 100_000  # single trade premium > $100K = "block"

    for _, row in trades_df.iterrows():
        try:
            iid    = int(row.get("instrument_id"))
            meta   = inst_map.get(iid)
            if not meta:
                continue

            price = float(row.get("price", 0))
            size  = int(row.get("size", 0))
            if price <= 0 or size <= 0:
                continue

            # Notional premium = price × size × 100 (contract multiplier)
            premium = price * size * 100

            if meta["type"] == "call":
                call_premium += premium
                call_volume  += size
                if premium >= BLOCK_THRESHOLD_USD:
                    call_blocks += 1
            else:
                put_premium += premium
                put_volume  += size
                if premium >= BLOCK_THRESHOLD_USD:
                    put_blocks += 1

            key = (meta["strike"], meta["type"])
            strike_premium[key] = strike_premium.get(key, 0.0) + premium

            # Aggressor classification using quotes (if we have them)
            # Trade hitting bid = sell-aggressor; lifting offer = buy-aggressor.
            # Without quotes, use the "side" field if Databento provides it.
            side = row.get("side")
            is_buy_aggressor = None
            if side == "A":   # ask side hit = buy aggressor
                is_buy_aggressor = True
            elif side == "B":  # bid side hit = sell aggressor
                is_buy_aggressor = False

            if is_buy_aggressor is True:
                if meta["type"] == "call":
                    call_buy_prem += premium
                else:
                    put_buy_prem += premium
            elif is_buy_aggressor is False:
                if meta["type"] == "call":
                    call_sell_prem += premium
                else:
                    put_sell_prem += premium

        except Exception:
            continue

    # Top 10 strikes by premium
    top_strikes = sorted(
        [{"strike": k[0], "type": k[1], "premium": round(v, 0)}
         for k, v in strike_premium.items()],
        key=lambda x: -x["premium"]
    )[:10]

    et = pytz.timezone("America/New_York")
    summary = {
        "symbol":            symbol,
        "flow_date":         flow_date_str,
        "call_premium":      round(call_premium, 0),
        "put_premium":       round(put_premium, 0),
        "call_volume":       call_volume,
        "put_volume":        put_volume,
        "call_buy_premium":  round(call_buy_prem, 0),
        "call_sell_premium": round(call_sell_prem, 0),
        "put_buy_premium":   round(put_buy_prem, 0),
        "put_sell_premium":  round(put_sell_prem, 0),
        "call_blocks":       call_blocks,
        "put_blocks":        put_blocks,
        "top_strikes":       top_strikes,
        "stored_at":         datetime.now(et).isoformat(),
    }

    _store_flow(summary)
    return summary


def _store_flow(s):
    conn = db_utils.connect(FLOW_DB)
    c    = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO flow_summaries
        (symbol, flow_date, call_premium, put_premium,
         call_volume, put_volume,
         call_buy_premium, call_sell_premium,
         put_buy_premium,  put_sell_premium,
         call_blocks, put_blocks, top_strikes, stored_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        s["symbol"], s["flow_date"],
        s["call_premium"], s["put_premium"],
        s["call_volume"], s["put_volume"],
        s["call_buy_premium"], s["call_sell_premium"],
        s["put_buy_premium"],  s["put_sell_premium"],
        s["call_blocks"], s["put_blocks"],
        json.dumps(s["top_strikes"]),
        s["stored_at"],
    ))
    conn.commit()
    conn.close()


def load_flow(symbol, flow_date):
    if hasattr(flow_date, "isoformat"):
        flow_date = flow_date.isoformat()
    conn = db_utils.connect(FLOW_DB)
    c    = conn.cursor()
    c.execute("""
        SELECT call_premium, put_premium, call_volume, put_volume,
               call_buy_premium, call_sell_premium,
               put_buy_premium,  put_sell_premium,
               call_blocks, put_blocks, top_strikes, stored_at
        FROM flow_summaries
        WHERE symbol = ? AND flow_date = ?
    """, (symbol, flow_date))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "symbol":            symbol,
        "flow_date":         flow_date,
        "call_premium":      row[0],
        "put_premium":       row[1],
        "call_volume":       row[2],
        "put_volume":        row[3],
        "call_buy_premium":  row[4],
        "call_sell_premium": row[5],
        "put_buy_premium":   row[6],
        "put_sell_premium":  row[7],
        "call_blocks":       row[8],
        "put_blocks":        row[9],
        "top_strikes":       json.loads(row[10] or "[]"),
        "stored_at":         row[11],
    }


# =============================================
# SIGNAL INTERPRETATION
# =============================================

def classify_flow(flow_data):
    """
    Translates raw flow into a directional bias.

    Returns:
      {
        "label":      "CALL_AGGRESSIVE" / "PUT_AGGRESSIVE" / "MIXED" / "STALE"
        "imbalance":  -1..+1
        "note":       human-readable
        "grade_pts":  0-15 grade contribution
      }
    """
    if not flow_data or flow_data.get("call_premium") is None:
        return {
            "label": "STALE", "imbalance": 0.0,
            "note": "No flow data yet for today", "grade_pts": 0,
        }

    call_prem = flow_data["call_premium"]
    put_prem  = flow_data["put_premium"]
    total     = call_prem + put_prem

    if total < 1_000_000:
        # Less than $1M total opening flow = too thin to signal anything
        return {
            "label": "MIXED", "imbalance": 0.0,
            "note": "Opening flow too thin (${:,.0f}) — no signal".format(total),
            "grade_pts": 0,
        }

    imbalance = (call_prem - put_prem) / total

    # Aggressor confirmation — if we have side data, prefer buy-aggressed
    # premium over raw notional
    call_buy = flow_data.get("call_buy_premium", 0) or 0
    put_buy  = flow_data.get("put_buy_premium", 0)  or 0
    aggressor_total = call_buy + put_buy
    if aggressor_total > 500_000:
        agg_imbalance = (call_buy - put_buy) / aggressor_total
        # Weight average: aggressor data is more reliable
        imbalance = 0.4 * imbalance + 0.6 * agg_imbalance

    imbalance = round(imbalance, 3)

    if imbalance > 0.30:
        return {
            "label":     "CALL_AGGRESSIVE",
            "imbalance": imbalance,
            "note":      "Calls ${:,.0f} vs puts ${:,.0f} ({}+{} blocks)".format(
                call_prem, put_prem,
                flow_data.get("call_blocks", 0),
                flow_data.get("put_blocks", 0)),
            "grade_pts": min(15, int(imbalance * 20)),
        }
    elif imbalance < -0.30:
        return {
            "label":     "PUT_AGGRESSIVE",
            "imbalance": imbalance,
            "note":      "Puts ${:,.0f} vs calls ${:,.0f} ({}+{} blocks)".format(
                put_prem, call_prem,
                flow_data.get("put_blocks", 0),
                flow_data.get("call_blocks", 0)),
            "grade_pts": min(15, int(abs(imbalance) * 20)),
        }
    else:
        return {
            "label":     "MIXED",
            "imbalance": imbalance,
            "note":      "Balanced flow (imbalance {:+.2f})".format(imbalance),
            "grade_pts": 0,
        }
