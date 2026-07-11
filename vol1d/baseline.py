# vol1d/baseline.py
# Time-of-day baseline + vix1d_tod_z (spec §2 — MANDATORY detrend).
#
# VIX1D drifts upward through the session BY CONSTRUCTION (0DTE time value
# bleeds, gamma dominates), so a raw level at 15:30 is not comparable to
# the same number at 09:45. Regime logic must key off vix1d_tod_z — the
# deviation from the median level for THIS minute of day over a trailing
# window of sessions — never the raw level. Skipping this makes the
# classifier read "expansive" every afternoon (the acceptance tripwire).
#
# Storage (vol1d_state.db, shared with vol1d.qa):
#   vol1d_ticks     one row per (session, minute): the proxy level the
#                   intraday updater computed. Feeds the nightly rebuild.
#   vol1d_baseline  per minute-of-day: median level, robust SD, and how
#                   many sessions contributed (the confidence input).

import statistics
from datetime import datetime, timedelta, timezone

import db_utils
from vol1d import config as vol1d_config

_DB = db_utils.data_path("vol1d_state.db")

# Keep raw ticks a bit past the lookback so the rebuild window is always
# fully covered; anything older is dead weight on the volume.
_TICK_RETENTION_MARGIN = 10

# 1.4826 * MAD estimates the SD of a normal sample while ignoring the fat
# tails vol-spike days put in the distribution.
_MAD_TO_SD = 1.4826


def _connect(db_path=None):
    conn = db_utils.connect(db_path or _DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vol1d_ticks (
            session_date  TEXT NOT NULL,
            minute_of_day INTEGER NOT NULL,
            level         REAL NOT NULL,
            PRIMARY KEY (session_date, minute_of_day)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vol1d_baseline (
            minute_of_day INTEGER PRIMARY KEY,
            median_level  REAL,
            sd            REAL,
            n_sessions    INTEGER,
            built_at      TEXT
        )
    """)
    return conn


def minute_of_day(ts_et):
    return ts_et.hour * 60 + ts_et.minute


def record_tick(ts_et, level, db_path=None):
    """Store one computed proxy level. Last write per (session, minute)
    wins, so the ~15s updater can call this every pass."""
    record_ticks([(ts_et, level)], db_path)


def record_ticks(pairs, db_path=None):
    """Bulk variant of record_tick for replay/backfill: one transaction for
    an iterable of (ts_et, level) pairs."""
    conn = _connect(db_path)
    conn.executemany("""
        INSERT OR REPLACE INTO vol1d_ticks (session_date, minute_of_day, level)
        VALUES (?, ?, ?)
    """, [(ts.strftime("%Y-%m-%d"), minute_of_day(ts), float(lv))
          for ts, lv in pairs])
    conn.commit()
    conn.close()


def sessions_banked(db_path=None):
    conn = _connect(db_path)
    n = conn.execute(
        "SELECT COUNT(DISTINCT session_date) FROM vol1d_ticks").fetchone()[0]
    conn.close()
    return int(n or 0)


def rebuild_baseline(cfg=None, db_path=None, now_utc=None):
    """Nightly job: median + robust SD per minute-of-day over the trailing
    lookback_sessions. Also prunes ticks past the retention window.
    Returns the number of minutes with a baseline row."""
    cfg = cfg or vol1d_config.get_config()
    lookback = cfg["tod_baseline"]["lookback_sessions"]

    conn = _connect(db_path)
    sessions = [r[0] for r in conn.execute(
        "SELECT DISTINCT session_date FROM vol1d_ticks "
        "ORDER BY session_date DESC LIMIT ?", (lookback,))]
    if not sessions:
        conn.close()
        return 0

    rows = conn.execute(
        "SELECT minute_of_day, level FROM vol1d_ticks "
        "WHERE session_date IN ({})".format(",".join("?" * len(sessions))),
        sessions).fetchall()

    by_minute = {}
    for minute, level in rows:
        by_minute.setdefault(minute, []).append(level)

    built_at = (now_utc or datetime.now(timezone.utc)).isoformat()
    min_sd = cfg["tod_baseline"].get("min_sd", 0.25)
    conn.execute("DELETE FROM vol1d_baseline")
    for minute, levels in by_minute.items():
        med = statistics.median(levels)
        if len(levels) >= 2:
            mad = statistics.median(abs(x - med) for x in levels)
            sd = _MAD_TO_SD * mad if mad > 0 else statistics.stdev(levels)
        else:
            sd = 0.0
        conn.execute("""
            INSERT OR REPLACE INTO vol1d_baseline
            (minute_of_day, median_level, sd, n_sessions, built_at)
            VALUES (?, ?, ?, ?, ?)
        """, (minute, med, max(sd, min_sd), len(levels), built_at))

    # Prune ticks beyond the retention window.
    keep = [r[0] for r in conn.execute(
        "SELECT DISTINCT session_date FROM vol1d_ticks "
        "ORDER BY session_date DESC LIMIT ?",
        (lookback + _TICK_RETENTION_MARGIN,))]
    if keep:
        conn.execute(
            "DELETE FROM vol1d_ticks WHERE session_date < ?", (min(keep),))

    conn.commit()
    conn.close()
    return len(by_minute)


def _baseline_row(conn, minute):
    """Baseline at `minute`, else the nearest minute with data (early/late
    prints and DB gaps clamp to the closest curve point)."""
    row = conn.execute(
        "SELECT median_level, sd, n_sessions FROM vol1d_baseline "
        "WHERE minute_of_day = ?", (minute,)).fetchone()
    if row:
        return row
    return conn.execute(
        "SELECT median_level, sd, n_sessions FROM vol1d_baseline "
        "ORDER BY ABS(minute_of_day - ?) LIMIT 1", (minute,)).fetchone()


def tod_z(ts_et, level, cfg=None, db_path=None):
    """(z, n_sessions) for `level` at this minute of day, or (None, 0)
    when no baseline exists yet (warmup). Callers treat n_sessions <
    tod_baseline.min_sessions as low-confidence, not as no-signal."""
    conn = _connect(db_path)
    row = _baseline_row(conn, minute_of_day(ts_et))
    conn.close()
    if not row or row[0] is None or not row[1]:
        return None, 0
    med, sd, n = row
    return round((level - med) / sd, 3), int(n or 0)
