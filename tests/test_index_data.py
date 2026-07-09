# Offline tests for index_data: CSV parsing, source fallback ordering,
# last-good cache, and prev_session's live-partial-bar skip.

from datetime import date, datetime, timedelta

import index_data


def _recent_rows(n=3, last_close=6300.0):
    """Rows ending yesterday (UTC) so _rows_ok freshness passes."""
    rows = []
    for i in range(n, 0, -1):
        d = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        c = last_close - (i - 1) * 10
        rows.append({"date": d, "o": c - 5, "h": c + 10, "l": c - 15, "c": c})
    return rows


def test_parse_cboe_history():
    text = ("DATE,OPEN,HIGH,LOW,CLOSE\n"
            "07/07/2026,6280.10,6310.55,6270.00,6300.25\n"
            "07/08/2026,6301.00,6320.00,6295.50,6315.75\n")
    recs = index_data.parse_cboe_history(text)
    assert recs == [
        ("2026-07-07", 6280.10, 6310.55, 6270.00, 6300.25),
        ("2026-07-08", 6301.00, 6320.00, 6295.50, 6315.75),
    ]


def test_parse_cboe_history_tolerates_header_drift_and_bad_rows():
    text = (" Date , Open , High , Low , Close \n"
            "07/08/2026,6301.00,6320.00,6295.50,6315.75\n"
            "garbage,x,y,z,w\n")
    recs = index_data.parse_cboe_history(text)
    assert len(recs) == 1
    assert recs[0][0] == "2026-07-08"


def test_parse_stooq_history():
    text = ("Date,Open,High,Low,Close,Volume\n"
            "2026-07-08,6301.0,6320.0,6295.5,6315.75,0\n")
    recs = index_data.parse_stooq_history(text)
    assert recs == [("2026-07-08", 6301.0, 6320.0, 6295.5, 6315.75)]


def test_rows_ok_rejects_stale_and_out_of_bounds():
    stale = [{"date": "2020-01-02", "o": 3200, "h": 3250, "l": 3150, "c": 3230}]
    assert not index_data._rows_ok(stale, "SPX")
    bogus = _recent_rows(last_close=63.0)   # mis-decimaled feed
    assert not index_data._rows_ok(bogus, "SPX")
    good = _recent_rows()
    assert index_data._rows_ok(good, "SPX")


def test_snapshot_falls_through_sources(monkeypatch):
    rows = _recent_rows()
    monkeypatch.setattr(index_data, "_FETCHERS",
                        (lambda idx: None,          # cboe down
                         lambda idx: rows,          # yahoo answers
                         lambda idx: None))
    snap = index_data.get_index_snapshot("SPX", force_refresh=True)
    assert snap["rows"] == rows
    assert snap["index"] == "SPX"


def test_snapshot_uses_last_good_cache_when_all_sources_fail(monkeypatch):
    rows = _recent_rows()
    index_data._cache_set("NDX", rows)
    monkeypatch.setattr(index_data, "_FETCHERS",
                        (lambda idx: None, lambda idx: None, lambda idx: None))
    snap = index_data.get_index_snapshot("NDX", force_refresh=True)
    assert snap is not None
    assert snap["source"] == "cache"
    assert snap["rows"] == rows


def test_snapshot_returns_none_when_everything_fails(monkeypatch):
    monkeypatch.setattr(index_data, "_FETCHERS",
                        (lambda idx: None, lambda idx: None, lambda idx: None))
    monkeypatch.setattr(index_data, "_cache_get", lambda idx: None)
    assert index_data.get_index_snapshot("SPX", force_refresh=True) is None


def test_prev_session_skips_live_partial_bar(monkeypatch):
    today = date(2026, 7, 9)
    rows = [
        {"date": "2026-07-07", "o": 6280.0, "h": 6310.0, "l": 6270.0, "c": 6300.0},
        {"date": "2026-07-08", "o": 6301.0, "h": 6320.0, "l": 6295.0, "c": 6315.0},
        {"date": "2026-07-09", "o": 6316.0, "h": 6330.0, "l": 6310.0, "c": 6325.0},
    ]
    monkeypatch.setattr(index_data, "get_index_snapshot",
                        lambda idx, force_refresh=False:
                        {"index": idx, "source": "cboe", "rows": rows})
    prev = index_data.prev_session("SPX", today_et=today)
    assert prev["date"] == "2026-07-08"      # not the live 07-09 bar
    assert prev["c"] == 6315.0
    assert prev["source"] == "cboe"
