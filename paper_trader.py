# paper_trader.py
# Synthetic backtest of today's signals against today's 5-min bars.
#
# Replaces the manual-logging requirement for the AI improvement loop:
# every signal the scanner emits gets walked forward through the day's bars,
# stamped with WIN/LOSS/EOD, and inserted into the trades table with
# mode='paper'. The existing run_ai_improvement reads from trades, so paper
# rows feed it automatically.
#
# Conventions:
#   - Entry bar is excluded; we walk bars STRICTLY AFTER the signal timestamp
#     (the bar containing the signal is the entry bar -- we entered intrabar
#     at "current price" close).
#   - Stop + target both touched in the same 5-min bar: stop-first
#     (penalizes 5-min granularity ambiguity rather than reward it).
#   - R-multiple uses underlying distance, not option premium:
#         r = (exit_under - entry_under) / |entry_under - stop_under|
#     (signed by direction). Option pnl is not modeled.

from datetime import datetime
import pytz

import db_utils
import data_fetcher


def init_paper_columns(db_file):
    """Idempotent schema migration: adds the columns the paper trader needs."""
    conn = db_utils.connect(db_file)
    for stmt in (
        "ALTER TABLE signals ADD COLUMN entry_under REAL",
        "ALTER TABLE signals ADD COLUMN und_stop REAL",
        "ALTER TABLE signals ADD COLUMN und_target_t1 REAL",
        "ALTER TABLE signals ADD COLUMN und_target_t2 REAL",
        "ALTER TABLE signals ADD COLUMN signal_type TEXT",
        "ALTER TABLE signals ADD COLUMN grade TEXT",
        "ALTER TABLE signals ADD COLUMN grade_pts INTEGER",
        "ALTER TABLE signals ADD COLUMN horizon TEXT",
        "ALTER TABLE trades ADD COLUMN mode TEXT DEFAULT 'real'",
        "ALTER TABLE trades ADD COLUMN horizon TEXT",
        "ALTER TABLE trades ADD COLUMN entry_hour REAL",
    ):
        try:
            conn.execute(stmt)
        except Exception:
            pass   # column already exists
    conn.commit()
    conn.close()


def _parse_iso(s):
    """Parse ISO-8601 string tolerantly. Returns datetime or None."""
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def walk_bars(bars_after_entry, direction, entry_under, stop_under, target_under):
    """
    Determine the outcome of a hypothetical trade by walking forward through
    5-min OHLC bars after entry.

    Returns (outcome, exit_under, r_multiple), or None if inputs are invalid.
      outcome: 'WIN' | 'LOSS' | 'EOD'
      r_multiple: signed; +1.5 means took 1.5R, -1.0 means full stop, -0.3
        means EOD closed at 30% adverse of risk.
    """
    if direction not in ("CALL", "PUT"):
        return None
    if entry_under is None or stop_under is None or target_under is None:
        return None
    risk = abs(entry_under - stop_under)
    if risk <= 0:
        return None
    reward = abs(target_under - entry_under)

    for b in bars_after_entry:
        hi, lo = b.get("h"), b.get("l")
        if hi is None or lo is None:
            continue
        if direction == "CALL":
            stop_hit   = lo <= stop_under
            target_hit = hi >= target_under
        else:
            stop_hit   = hi >= stop_under
            target_hit = lo <= target_under

        if stop_hit and target_hit:
            # Conservative: assume stop touched first.
            return ("LOSS", float(stop_under), -1.0)
        if stop_hit:
            return ("LOSS", float(stop_under), -1.0)
        if target_hit:
            return ("WIN", float(target_under), round(reward / risk, 2))

    if not bars_after_entry:
        return None

    close = bars_after_entry[-1].get("c")
    if close is None:
        return None
    if direction == "CALL":
        signed_r = (close - entry_under) / risk
    else:
        signed_r = (entry_under - close) / risk
    return ("EOD", float(close), round(signed_r, 2))


def _entry_hour_et(ts_iso):
    """Signal timestamp -> fractional ET hour (e.g. 10.58 for 10:35 ET).

    Paper rows used to leave entry_hour NULL, and the AI stats layer
    defaulted NULL to 9.5 -- so every paper trade landed in the
    9:30-10:00 bucket and the by_time win rates were fiction.
    """
    dt = _parse_iso(ts_iso)
    if dt is None or dt.tzinfo is None:
        return None
    et = dt.astimezone(pytz.timezone("America/New_York"))
    return round(et.hour + et.minute / 60.0, 2)


def _bars_after(symbol, signal_ts_iso):
    sig_dt = _parse_iso(signal_ts_iso)
    if sig_dt is None:
        return []
    bars = data_fetcher.get_intraday(symbol)
    if not bars:
        return []
    out = []
    for b in bars:
        bdt = _parse_iso(b.get("t", ""))
        if bdt is not None and bdt > sig_dt:
            out.append(b)
    return out


def _todays_signals(conn):
    et = pytz.timezone("America/New_York")
    today = datetime.now(et).strftime("%Y-%m-%d")
    c = conn.cursor()
    c.execute("""
        SELECT id, ts, symbol, direction, price, score, premium, contracts,
               stop, target,
               entry_under, und_stop, und_target_t1, und_target_t2,
               signal_type, grade, grade_pts, horizon
        FROM signals
        WHERE substr(ts, 1, 10) = ?
    """, (today,))
    return c.fetchall()


def _already_paper_traded(conn, signal_id, target_kind):
    c = conn.cursor()
    c.execute("""
        SELECT id FROM trades
        WHERE mode = 'paper' AND signal_type = ?
        LIMIT 1
    """, ("paper:{}:{}".format(signal_id, target_kind),))
    return c.fetchone() is not None


def _insert_paper_trade(conn, sig, target_kind, target_under,
                         outcome, exit_under, r_mult):
    (sig_id, ts, symbol, direction, price, score, premium, contracts,
     stop, target,
     entry_under, und_stop, und_t1, und_t2,
     signal_type, grade, grade_pts, horizon) = sig

    c = conn.cursor()
    c.execute("""
        INSERT INTO trades
        (ts, symbol, direction, premium, contracts, stop, target, outcome,
         exit_price, pnl, r_mult, grade, grade_pts, entry_under, signal_type,
         mode, horizon, entry_hour)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ts, symbol, direction, premium, contracts, und_stop, target_under,
        outcome, round(exit_under, 4), None, r_mult,
        grade, grade_pts, entry_under,
        "paper:{}:{}".format(sig_id, target_kind),
        "paper", horizon, _entry_hour_et(ts),
    ))
    conn.commit()


def run_paper_trader(db_file, log_fn=None):
    """
    Replay today's signals through today's 5-min bars and insert WIN/LOSS/EOD
    outcomes for both T1 and T2 targets. Returns (inserted, skipped) counts.

    Idempotent within the day -- already-evaluated (signal_id, target_kind)
    combinations are skipped.
    """
    log = log_fn or (lambda *_: None)

    conn = db_utils.connect(db_file)
    try:
        signals = _todays_signals(conn)
        inserted = 0
        skipped  = 0

        for sig in signals:
            (sig_id, ts, symbol, direction, price, score, premium, contracts,
             stop, target,
             entry_under, und_stop, und_t1, und_t2,
             signal_type, grade, grade_pts, horizon) = sig

            if direction not in ("CALL", "PUT"):
                skipped += 1
                continue
            if entry_under is None or und_stop is None:
                skipped += 1
                continue
            if not (und_t1 or und_t2):
                skipped += 1
                continue

            bars = _bars_after(symbol, ts)
            if not bars:
                skipped += 1
                continue

            for kind, tval in (("T1", und_t1), ("T2", und_t2)):
                if not tval:
                    continue
                if _already_paper_traded(conn, sig_id, kind):
                    continue
                outcome = walk_bars(bars, direction, entry_under, und_stop, tval)
                if outcome is None:
                    continue
                outcome_kind, exit_under, r_mult = outcome
                _insert_paper_trade(conn, sig, kind, tval,
                                    outcome_kind, exit_under, r_mult)
                inserted += 1

        log("[paper] EOD run: signals={} inserted={} skipped={}".format(
            len(signals), inserted, skipped))
        return inserted, skipped
    finally:
        conn.close()
