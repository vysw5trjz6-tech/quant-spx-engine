# The time-of-day detrend is the acceptance tripwire for the whole vol1d
# module: VIX1D drifts up mechanically through every session, so a raw
# afternoon level that merely sits ON the usual drift curve must read
# z ~ 0 — NOT "expansive". These tests pin that.

import os
import random
import tempfile
from datetime import date, datetime, timedelta

from vol1d import baseline
from vol1d import config as vol1d_config


def _db():
    return os.path.join(tempfile.mkdtemp(prefix="vol1d-bl-"), "state.db")


def _drift_level(minute, base=15.0):
    """Deterministic intraday drift: rises ~4 vol pts 9:30 -> 16:00."""
    return base + (minute - 570) / 390.0 * 4.0


def _seed_sessions(db, n_sessions, jitter=0.0, base=15.0, start=date(2026, 4, 1)):
    rng = random.Random(42)
    d = start
    added = 0
    pairs = []
    while added < n_sessions:
        if d.weekday() < 5:
            for minute in range(570, 960, 5):        # 9:30..15:55 every 5 min
                ts = datetime(d.year, d.month, d.day, minute // 60, minute % 60)
                level = _drift_level(minute, base) + (rng.uniform(-jitter, jitter)
                                                      if jitter else 0.0)
                pairs.append((ts, level))
            added += 1
        d += timedelta(days=1)
    baseline.record_ticks(pairs, db_path=db)
    return d


def test_afternoon_drift_reads_flat_z():
    # 30 sessions of the pure drift curve. An afternoon print ON the curve
    # is high in raw terms (~18.1 vs the 15.0 open) but must z out to ~0.
    db = _db()
    _seed_sessions(db, 30, jitter=0.4)
    assert baseline.rebuild_baseline(db_path=db) > 0

    afternoon = datetime(2026, 7, 8, 15, 30)
    on_curve = _drift_level(baseline.minute_of_day(afternoon))
    z, n = baseline.tod_z(afternoon, on_curve, db_path=db)
    assert n == 30
    assert abs(z) < 1.0, "on-drift afternoon level must NOT read expansive"

    # The same absolute level at the OPEN is genuinely elevated (+3 pts
    # over the morning median) and must read strongly positive.
    open_ts = datetime(2026, 7, 8, 9, 35)
    z_open, _ = baseline.tod_z(open_ts, on_curve, db_path=db)
    assert z_open > 2.0


def test_z_sign_and_magnitude():
    db = _db()
    _seed_sessions(db, 20)            # zero jitter: median exactly on curve
    baseline.rebuild_baseline(db_path=db)
    ts = datetime(2026, 7, 8, 11, 0)
    med = _drift_level(baseline.minute_of_day(ts))
    z_hi, _ = baseline.tod_z(ts, med + 2.0, db_path=db)
    z_lo, _ = baseline.tod_z(ts, med - 2.0, db_path=db)
    assert z_hi > 0 > z_lo
    assert abs(z_hi + z_lo) < 1e-9    # symmetric around the median


def test_warmup_returns_none_without_baseline():
    db = _db()
    z, n = baseline.tod_z(datetime(2026, 7, 8, 10, 0), 17.0, db_path=db)
    assert z is None and n == 0


def test_lookback_excludes_older_sessions():
    # 10 old sessions at a 25-vol base, then 20 recent at 15. With
    # lookback=20 the baseline must reflect only the recent regime.
    db = _db()
    end_of_old = _seed_sessions(db, 10, base=25.0)
    _seed_sessions(db, 20, base=15.0, start=end_of_old)

    cfg = vol1d_config.get_config()
    cfg["tod_baseline"]["lookback_sessions"] = 20
    baseline.rebuild_baseline(cfg=cfg, db_path=db)

    ts = datetime(2026, 7, 8, 12, 0)
    z, n = baseline.tod_z(ts, _drift_level(baseline.minute_of_day(ts), 15.0),
                          db_path=db)
    assert n == 20
    assert abs(z) < 1.0, "old 25-vol regime must not drag the baseline up"


def test_min_sd_floor_prevents_z_blowup():
    # Zero-jitter sessions -> raw SD ~ 0; the floor keeps a 0.3-pt wiggle
    # from exploding into a giant z.
    db = _db()
    _seed_sessions(db, 20, jitter=0.0)
    baseline.rebuild_baseline(db_path=db)
    ts = datetime(2026, 7, 8, 13, 0)
    z, _ = baseline.tod_z(ts, _drift_level(baseline.minute_of_day(ts)) + 0.3,
                          db_path=db)
    assert abs(z) <= 0.3 / 0.25 + 1e-9


def test_nearest_minute_clamp():
    db = _db()
    _seed_sessions(db, 5)
    baseline.rebuild_baseline(db_path=db)
    # 16:30 is past the last banked minute (15:55) -> clamps to the nearest
    # curve point instead of returning nothing.
    z, n = baseline.tod_z(datetime(2026, 7, 8, 16, 30),
                          _drift_level(955), db_path=db)
    assert z is not None and n == 5


def test_record_tick_last_write_wins():
    db = _db()
    ts = datetime(2026, 7, 8, 10, 0, 5)
    baseline.record_tick(ts, 15.0, db_path=db)
    baseline.record_tick(ts.replace(second=50), 16.0, db_path=db)
    baseline.rebuild_baseline(db_path=db)
    z, n = baseline.tod_z(ts, 16.0, db_path=db)
    assert n == 1 and z == 0.0
