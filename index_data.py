# index_data.py
# Real SPX / NDX index levels — replaces the fixed-multiplier SPY/QQQ proxy.
#
# The engine trades ETFs but frames everything against the cash indexes.
# Until now the "index" numbers were SPY x 10 / QQQ x 41, which drifts with
# the ETF's expense/dividend basis and can't be used as real option-strike
# levels. This module fetches the actual index daily OHLC so the premarket
# brief and dashboard can quote genuine SPX/NDX points.
#
# Source priority (same layering that made the VIX read reliable):
#   1. CBOE official daily index CSV  -- authoritative, key-less,
#      datacenter-friendly (SPX only; NDX is a Nasdaq index CBOE doesn't carry)
#   2. Yahoo Finance (^GSPC / ^NDX)   -- real index, often blocked on cloud IPs
#   3. Stooq free CSV (^spx / ^ndx)   -- key-less mirror
#   4. Persistent last-good cache     -- survives a full outage
#
# All values are daily bars: the last COMPLETED session is what the 9:10 AM
# premarket brief needs (yesterday's close/high/low). During RTH some sources
# append a live partial bar for today; prev_session() skips it.

import io
import csv
import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone

import pytz

try:
    import db_utils
    _HAS_DB = True
except ImportError:
    _HAS_DB = False

INDEXES = ("SPX", "NDX")

_CBOE_URL = ("https://cdn.cboe.com/api/global/us_indices/"
             "daily_prices/{}_History.csv")
_CBOE_SYMBOLS  = {"SPX": "SPX"}                       # NDX not published by CBOE
_YAHOO_SYMBOLS = {"SPX": "^GSPC", "NDX": "^NDX"}
_STOOQ_SYMBOLS = {"SPX": "%5Espx", "NDX": "%5Endx"}

# Sanity bounds: reject obviously-wrong values (a mis-parsed column, a
# mis-decimaled mirror) before they poison gap math downstream.
_LEVEL_BOUNDS = {"SPX": (1500, 30000), "NDX": (5000, 80000)}

# A last row older than this means the source silently froze; fall through.
_MAX_STALE_DAYS = 6

_CACHE_DB = ((os.getenv("DATA_DIR")
              or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
              or "/tmp").rstrip("/") + "/index_cache.db")
_CACHE_MAX_AGE = timedelta(days=5)

# In-memory TTL cache: the dashboard renders index context on every page
# load, and these are slow remote CSV pulls. Daily bars only change once a
# day, so 15 minutes is generous.
_MEM_TTL  = 15 * 60
_mem_cache = {}   # index -> (fetched_at_epoch, snapshot_dict_or_None)


def _rows_ok(rows, index):
    """Basic sanity: non-empty, fresh, and levels inside plausible bounds."""
    if not rows:
        return False
    lo, hi = _LEVEL_BOUNDS.get(index, (0, float("inf")))
    last = rows[-1]
    try:
        last_date = datetime.strptime(last["date"], "%Y-%m-%d")
    except (KeyError, ValueError):
        return False
    age = (datetime.now(timezone.utc).replace(tzinfo=None) - last_date).days
    if age > _MAX_STALE_DAYS:
        return False
    for r in rows:
        if not (lo <= r["c"] <= hi):
            return False
    return True


def _tail_rows(records, n=3):
    """Normalize parsed (date, o, h, l, c) tuples into the last n row dicts."""
    rows = []
    for date_s, o, h, l, c in records[-n:]:
        rows.append({
            "date": date_s,
            "o": round(o, 2), "h": round(h, 2),
            "l": round(l, 2), "c": round(c, 2),
        })
    return rows


def parse_cboe_history(text):
    """
    Parse CBOE's index history CSV (header: DATE,OPEN,HIGH,LOW,CLOSE with
    DATE like MM/DD/YYYY) into [(iso_date, o, h, l, c), ...] ascending.
    Tolerates header whitespace/case drift like the VIX reader does.
    """
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        keys = {k.strip().upper(): k for k in row if k}
        try:
            d = datetime.strptime(row[keys["DATE"]].strip(), "%m/%d/%Y")
            out.append((
                d.strftime("%Y-%m-%d"),
                float(row[keys["OPEN"]]),
                float(row[keys["HIGH"]]),
                float(row[keys["LOW"]]),
                float(row[keys["CLOSE"]]),
            ))
        except (KeyError, ValueError):
            continue
    return out


def parse_stooq_history(text):
    """Parse Stooq's daily CSV (Date,Open,High,Low,Close,...) ascending."""
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            out.append((
                row["Date"].strip(),
                float(row["Open"]),
                float(row["High"]),
                float(row["Low"]),
                float(row["Close"]),
            ))
        except (KeyError, ValueError):
            continue
    return out


def _fetch_cboe(index):
    sym = _CBOE_SYMBOLS.get(index)
    if not sym:
        return None
    try:
        r = requests.get(_CBOE_URL.format(sym), timeout=15)
        if r.status_code != 200 or not r.text:
            return None
        rows = _tail_rows(parse_cboe_history(r.text))
        return rows if _rows_ok(rows, index) else None
    except Exception as e:
        print("[index_data] CBOE {} fetch failed: {}".format(index, e))
        return None


def _fetch_yahoo(index):
    ticker = _YAHOO_SYMBOLS.get(index)
    if not ticker:
        return None
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="7d", interval="1d")
        if hist is None or hist.empty:
            return None
        records = []
        for ts, row in hist.iterrows():
            try:
                records.append((
                    ts.strftime("%Y-%m-%d"),
                    float(row["Open"]), float(row["High"]),
                    float(row["Low"]),  float(row["Close"]),
                ))
            except (KeyError, ValueError, TypeError):
                continue
        rows = _tail_rows(records)
        return rows if _rows_ok(rows, index) else None
    except ImportError:
        return None
    except Exception as e:
        print("[index_data] Yahoo {} fetch failed: {}".format(index, e))
        return None


def _fetch_stooq(index):
    sym = _STOOQ_SYMBOLS.get(index)
    if not sym:
        return None
    try:
        r = requests.get("https://stooq.com/q/d/l/?s={}&i=d".format(sym),
                         timeout=10)
        if r.status_code != 200 or not r.text:
            return None
        rows = _tail_rows(parse_stooq_history(r.text))
        return rows if _rows_ok(rows, index) else None
    except Exception as e:
        print("[index_data] Stooq {} fetch failed: {}".format(index, e))
        return None


# =============================================
# PERSISTENT LAST-GOOD CACHE
# =============================================

def _cache_init(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS index_last_good "
                 "(idx TEXT PRIMARY KEY, rows_json TEXT, stored_at TEXT)")


def _cache_set(index, rows):
    if not _HAS_DB:
        return
    try:
        conn = db_utils.connect(_CACHE_DB)
        _cache_init(conn)
        conn.execute("INSERT OR REPLACE INTO index_last_good "
                     "(idx, rows_json, stored_at) VALUES (?, ?, ?)",
                     (index, json.dumps(rows),
                      datetime.now(timezone.utc).isoformat()))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _cache_get(index):
    if not _HAS_DB:
        return None
    try:
        conn = db_utils.connect(_CACHE_DB)
        _cache_init(conn)
        row = conn.execute("SELECT rows_json, stored_at FROM index_last_good "
                           "WHERE idx = ?", (index,)).fetchone()
        conn.close()
        if not row:
            return None
        stored = datetime.fromisoformat(row[1])
        if datetime.now(timezone.utc) - stored > _CACHE_MAX_AGE:
            return None
        return json.loads(row[0])
    except Exception:
        return None


# =============================================
# PUBLIC API
# =============================================

_FETCHERS = (_fetch_cboe, _fetch_yahoo, _fetch_stooq)


def get_index_snapshot(index, force_refresh=False):
    """
    Latest real daily bars for a cash index ('SPX' | 'NDX').

    Returns {"index", "source", "rows": [{date,o,h,l,c}, ...]} with rows
    ascending (up to 3, most recent last), or None when every source and
    the last-good cache fail.
    """
    index = index.upper()
    now = time.time()
    if not force_refresh:
        hit = _mem_cache.get(index)
        if hit and now - hit[0] < _MEM_TTL:
            return hit[1]

    snapshot = None
    for fetch in _FETCHERS:
        rows = fetch(index)
        if rows:
            snapshot = {
                "index":  index,
                "source": fetch.__name__.replace("_fetch_", ""),
                "rows":   rows,
            }
            _cache_set(index, rows)
            break

    if snapshot is None:
        rows = _cache_get(index)
        if rows:
            print("[index_data] using cached last-good {} rows".format(index))
            snapshot = {"index": index, "source": "cache", "rows": rows}

    _mem_cache[index] = (now, snapshot)
    return snapshot


def prev_session(index, today_et=None):
    """
    The most recent COMPLETED session bar for the index:
    {"date", "o", "h", "l", "c", "source"} or None.

    Skips a live partial bar for `today_et` (Yahoo appends one during RTH);
    at 9:10 AM ET this is simply yesterday's bar.
    """
    snap = get_index_snapshot(index)
    if not snap:
        return None
    if today_et is None:
        et = pytz.timezone("America/New_York")
        today_et = datetime.now(et).date()
    today_iso = today_et.isoformat()
    for row in reversed(snap["rows"]):
        if row["date"] < today_iso:
            out = dict(row)
            out["source"] = snap["source"]
            return out
    return None
