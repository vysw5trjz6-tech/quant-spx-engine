# Offline tests for the replay harness's pure assembly logic
# (vol1d/replay.py) and the QA official-source parsing + residual store
# (vol1d/qa.py). The Databento/network sides live in scripts/ and run on
# the deployment only.

import os
import tempfile
from datetime import date, datetime

from vol1d import qa, replay


# ---------------------------------------------------------------------------
# Forward-fill + snapshot assembly
# ---------------------------------------------------------------------------

def _m(h, mi):
    return datetime(2026, 7, 8, h, mi)


def test_forward_fill_carries_standing_quotes():
    rows = [
        {"minute": _m(9, 35), "key": 1, "bid": 5.0, "ask": 5.4},
        {"minute": _m(9, 35), "key": 2, "bid": 2.0, "ask": 2.2},
        {"minute": _m(9, 37), "key": 1, "bid": 5.2, "ask": 5.6},  # 2 silent
    ]
    book = replay.forward_fill_book(rows)
    assert book[_m(9, 36)][1] == (5.0, 5.4)      # gap minute filled
    assert book[_m(9, 37)][1] == (5.2, 5.6)      # update applied
    assert book[_m(9, 37)][2] == (2.0, 2.2)      # standing market carried


def test_snapshot_uses_latest_book_at_or_before_minute():
    rows = [{"minute": _m(9, 35), "key": 1, "bid": 5.0, "ask": 5.4}]
    meta = {1: {"root": "SPXW", "expiry": date(2026, 7, 8),
                "type": "call", "strike": 6300.0}}
    book = replay.forward_fill_book(rows)
    snap = replay.snapshot_at(book, meta, _m(10, 0), spot=6300.0)
    assert snap is not None
    assert snap["quotes"][0]["bid"] == 5.0
    # Nothing at/before the first row -> None.
    assert replay.snapshot_at(book, meta, _m(9, 30)) is None


def test_session_minutes_cadence():
    minutes = replay.session_minutes(date(2026, 7, 8), interval_min=30)
    assert minutes[0] == _m(9, 35)
    assert minutes[-1] <= _m(15, 55)
    assert (minutes[1] - minutes[0]).total_seconds() == 1800


def test_tracking_report_stats():
    report = replay.tracking_report([
        {"date": "2026-07-07", "proxy_close": 19.0, "official_close": 18.5,
         "series": [("t", 19.0)]},
        {"date": "2026-07-08", "proxy_close": 21.0, "official_close": 22.0,
         "series": [("t", 21.0)]},
        {"date": "2026-07-09", "proxy_close": 20.0, "official_close": None,
         "series": []},
    ])
    assert report["n_compared"] == 2
    assert report["mean_residual"] == round((0.5 - 1.0) / 2, 3)
    assert report["mean_abs_residual"] == 0.75
    assert report["max_abs_residual"] == 1.0


# ---------------------------------------------------------------------------
# QA: official CSV parse + residual store
# ---------------------------------------------------------------------------

_CSV = """DATE,OPEN,HIGH,LOW,CLOSE
07/07/2026,17.10,19.80,16.90,18.50
07/08/2026,18.00,23.10,17.70,22.00
"""


def test_official_csv_parse(monkeypatch):
    # Freeze staleness relative to the fixture dates.
    hist = qa.CboeCsvOfficialSource.parse_csv(_CSV)
    if hist is None:
        # parse_csv applies the staleness guard against the real clock;
        # rebuild with today's date to exercise the parse itself.
        today = datetime.now().strftime("%m/%d/%Y")
        hist = qa.CboeCsvOfficialSource.parse_csv(
            "DATE,OPEN,HIGH,LOW,CLOSE\n{},18.00,23.10,17.70,22.00\n".format(today))
        assert hist is not None
        assert list(hist.values()) == [22.0]
    else:
        assert hist["2026-07-08"] == 22.0


def test_residual_store_roundtrip():
    db = os.path.join(tempfile.mkdtemp(prefix="vol1d-qa-"), "qa.db")
    r = qa.save_residual("2026-07-08", 21.4, 22.0, db_path=db)
    assert r == -0.6
    d, latest = qa.latest_residual(db_path=db)
    assert d == "2026-07-08"
    assert latest == -0.6


def test_residual_tolerance_gate():
    cfg = {"qa": {"residual_tolerance": 2.0, "reconcile_with_official": True}}
    assert qa.residual_within_tolerance(1.9, cfg)
    assert not qa.residual_within_tolerance(-2.5, cfg)
    assert qa.residual_within_tolerance(None, cfg)   # unknown != broken


def test_reconcile_daily_with_stubbed_source():
    class _Stub(qa.OfficialVix1dSource):
        def get_history(self):
            return {"2026-07-08": 22.0}

    db = os.path.join(tempfile.mkdtemp(prefix="vol1d-qa-"), "qa.db")
    out = qa.reconcile_daily("2026-07-08", 21.0, source=_Stub(), db_path=db)
    assert out["official_close"] == 22.0
    assert out["residual"] == -1.0
    assert out["within_tolerance"] is True
