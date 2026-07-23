# Offline tests for the overnight-bars provider chain in overnight_context:
# Databento → Alpaca overnight (IEX + Blue Ocean ATS) → Yahoo continuous
# futures → Alpaca premarket-only proxy. Alpaca overnight is preferred over
# Yahoo because Yahoo's scrape endpoints rate-limit datacenter (Railway) IPs;
# the boats leg gives the full 8 PM-4 AM overnight session the plain
# premarket proxy structurally misses.

import sys
import types
from datetime import date, datetime, timedelta

import pandas as pd
import pytest
import pytz

import overnight_context


TARGET = date(2026, 7, 14)  # a Tuesday; prev session Monday 2026-07-13


def _no_databento(monkeypatch):
    fake = types.ModuleType("databento_adapter")
    fake.is_available = lambda: False
    fake.get_overnight_bars = lambda *a, **k: []
    monkeypatch.setitem(sys.modules, "databento_adapter", fake)


def _futures_bars():
    return [
        {"t": "2026-07-13T20:15:00-04:00",
         "o": 7590.0, "h": 7611.0, "l": 7585.0, "c": 7601.0, "v": 1200},
        {"t": "2026-07-14T03:00:00-04:00",
         "o": 7601.0, "h": 7605.0, "l": 7570.0, "c": 7580.0, "v": 900},
    ]


# =============================================
# Provider chain ordering
# =============================================

def _overnight_proxy_bars():
    return [{"t": "2026-07-14T02:00:00Z",
             "o": 750.0, "h": 754.2, "l": 747.5, "c": 752.0, "v": 500}]


def test_alpaca_overnight_preferred_over_yahoo(monkeypatch):
    # New priority: Alpaca overnight (path 2) beats Yahoo (path 3), because
    # Yahoo is unreliable from datacenter IPs.
    _no_databento(monkeypatch)
    monkeypatch.setattr(overnight_context, "_fetch_alpaca_overnight",
                        lambda contract, s, e: _overnight_proxy_bars())

    def _boom(*a, **k):
        raise AssertionError("Yahoo must not be hit when Alpaca overnight works")
    monkeypatch.setattr(overnight_context, "_fetch_yahoo_futures", _boom)

    bars, source = overnight_context._get_overnight_bars(TARGET, "ES")
    assert source == "etf_proxy"
    assert max(b["h"] for b in bars) == 754.2


def test_yahoo_used_when_alpaca_overnight_empty(monkeypatch):
    _no_databento(monkeypatch)
    monkeypatch.setattr(overnight_context, "_fetch_alpaca_overnight",
                        lambda contract, s, e: [])
    monkeypatch.setattr(overnight_context, "_fetch_yahoo_futures",
                        lambda contract, s, e: _futures_bars())

    bars, source = overnight_context._get_overnight_bars(TARGET, "ES")
    assert source == "futures"
    assert max(b["h"] for b in bars) == 7611.0


def test_falls_back_to_premarket_proxy_when_all_else_empty(monkeypatch):
    _no_databento(monkeypatch)
    monkeypatch.setattr(overnight_context, "_fetch_alpaca_overnight",
                        lambda contract, s, e: [])
    monkeypatch.setattr(overnight_context, "_fetch_yahoo_futures",
                        lambda contract, s, e: [])
    proxy_bars = [{"t": "2026-07-14T08:00:00Z",
                   "o": 750.0, "h": 753.9, "l": 748.1, "c": 752.0, "v": 500}]
    monkeypatch.setattr(overnight_context, "_fetch_alpaca_extended_hours",
                        lambda symbol, s, e, feed="iex": proxy_bars)

    bars, source = overnight_context._get_overnight_bars(TARGET, "ES")
    assert source == "etf_proxy"
    assert bars == proxy_bars


def test_overnight_range_carries_futures_source(monkeypatch):
    _no_databento(monkeypatch)
    monkeypatch.setattr(overnight_context, "_fetch_alpaca_overnight",
                        lambda contract, s, e: [])
    monkeypatch.setattr(overnight_context, "_fetch_yahoo_futures",
                        lambda contract, s, e: _futures_bars())
    on = overnight_context.overnight_range(TARGET, "ES")
    assert on["source"] == "futures"
    assert on["high"] == 7611.0
    assert on["low"] == 7570.0


# =============================================
# _fetch_alpaca_overnight: stitch IEX + boats, dedupe
# =============================================

def test_alpaca_overnight_stitches_and_dedupes(monkeypatch):
    monkeypatch.setattr(overnight_context, "ALPACA_KEY", "k")
    monkeypatch.setattr(overnight_context, "ALPACA_SECRET", "s")

    postmarket = {"t": "2026-07-13T22:00:00Z",
                  "o": 750.0, "h": 751.0, "l": 749.0, "c": 750.5, "v": 100}
    boundary_iex = {"t": "2026-07-14T00:00:00Z",
                    "o": 750.5, "h": 752.0, "l": 750.0, "c": 751.5, "v": 80}
    overnight = {"t": "2026-07-14T03:00:00Z",
                 "o": 751.5, "h": 756.0, "l": 748.0, "c": 749.0, "v": 60}
    # boats returns the same boundary bar (dedupe) plus the deep overnight one.
    boundary_boats = dict(boundary_iex)

    def _fake(symbol, s, e, feed="iex"):
        assert symbol == "SPY"
        if feed == "iex":
            return [postmarket, boundary_iex]
        if feed == "boats":
            return [boundary_boats, overnight]
        raise AssertionError("unexpected feed")
    monkeypatch.setattr(overnight_context, "_fetch_alpaca_extended_hours", _fake)

    start_iso, end_iso = overnight_context.overnight_window(TARGET)
    bars = overnight_context._fetch_alpaca_overnight("ES", start_iso, end_iso)
    ts = [b["t"] for b in bars]
    assert ts == sorted(ts)                      # ascending
    assert len(bars) == 3                        # boundary deduped, not doubled
    assert max(b["h"] for b in bars) == 756.0    # overnight high captured


def test_alpaca_overnight_no_keys_returns_empty(monkeypatch):
    monkeypatch.setattr(overnight_context, "ALPACA_KEY", "")
    monkeypatch.setattr(overnight_context, "ALPACA_SECRET", "")

    def _boom(*a, **k):
        raise AssertionError("must not call the API without credentials")
    monkeypatch.setattr(overnight_context, "_fetch_alpaca_extended_hours",
                        _boom)
    start_iso, end_iso = overnight_context.overnight_window(TARGET)
    assert overnight_context._fetch_alpaca_overnight("ES", start_iso,
                                                     end_iso) == []


# =============================================
# _fetch_yahoo_futures: window clipping + sanity bounds
# =============================================

class _FakeTicker:
    def __init__(self, frame):
        self._frame = frame

    def history(self, **kwargs):
        return self._frame


def _install_fake_yfinance(monkeypatch, frame):
    fake = types.ModuleType("yfinance")
    fake.Ticker = lambda symbol: _FakeTicker(frame)
    monkeypatch.setitem(sys.modules, "yfinance", fake)


def _frame(rows):
    idx = [r[0] for r in rows]
    data = {
        "Open":   [r[1] for r in rows],
        "High":   [r[2] for r in rows],
        "Low":    [r[3] for r in rows],
        "Close":  [r[4] for r in rows],
        "Volume": [r[5] for r in rows],
    }
    return pd.DataFrame(data, index=pd.DatetimeIndex(idx))


def test_yahoo_fetch_clips_window_and_rejects_garbage(monkeypatch):
    et = pytz.timezone("America/New_York")
    start_iso, end_iso = overnight_context.overnight_window(TARGET)
    inside  = et.localize(datetime(2026, 7, 13, 22, 0))
    early   = et.localize(datetime(2026, 7, 13, 12, 0))   # before the window
    late    = et.localize(datetime(2026, 7, 14, 10, 0))   # after 9:30 open
    rows = [
        (early,  7500.0, 7505.0, 7495.0, 7500.0, 100),   # clipped: too early
        (inside, 7590.0, 7611.0, 7585.0, 7601.0, 1200),  # kept
        (inside + timedelta(minutes=5),
                 759.0,  761.1,  758.5,  760.1,  900),   # rejected: SPY-scale
        (inside + timedelta(minutes=10),
                 7601.0, 7570.0, 7605.0, 7580.0, 900),   # rejected: h < l
        (late,   7620.0, 7625.0, 7615.0, 7620.0, 300),   # clipped: post-open
    ]
    _install_fake_yfinance(monkeypatch, _frame(rows))

    bars = overnight_context._fetch_yahoo_futures("ES", start_iso, end_iso)
    assert len(bars) == 1
    assert bars[0]["h"] == 7611.0
    assert bars[0]["v"] == 1200


def test_yahoo_fetch_empty_frame_returns_empty(monkeypatch):
    start_iso, end_iso = overnight_context.overnight_window(TARGET)
    _install_fake_yfinance(monkeypatch, pd.DataFrame())
    assert overnight_context._fetch_yahoo_futures("ES", start_iso,
                                                  end_iso) == []


def test_yahoo_fetch_unknown_contract_returns_empty():
    start_iso, end_iso = overnight_context.overnight_window(TARGET)
    assert overnight_context._fetch_yahoo_futures("ZZ", start_iso,
                                                  end_iso) == []
