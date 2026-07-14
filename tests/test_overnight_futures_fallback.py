# Offline tests for the overnight-bars provider chain in overnight_context:
# Databento → Yahoo continuous futures → Alpaca ETF proxy. The Yahoo path is
# what keeps the premarket brief in REAL futures points (full Globex range)
# when Databento is down, instead of silently degrading to SPY premarket
# dollars that miss the true overnight high/low.

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

def test_yahoo_futures_preferred_over_etf_proxy(monkeypatch):
    _no_databento(monkeypatch)
    monkeypatch.setattr(overnight_context, "_fetch_yahoo_futures",
                        lambda contract, s, e: _futures_bars())

    def _boom(*a, **k):
        raise AssertionError("Alpaca proxy must not be hit when Yahoo works")
    monkeypatch.setattr(overnight_context, "_fetch_alpaca_extended_hours",
                        _boom)

    bars, source = overnight_context._get_overnight_bars(TARGET, "ES")
    assert source == "futures"
    assert max(b["h"] for b in bars) == 7611.0


def test_falls_back_to_etf_proxy_when_yahoo_empty(monkeypatch):
    _no_databento(monkeypatch)
    monkeypatch.setattr(overnight_context, "_fetch_yahoo_futures",
                        lambda contract, s, e: [])
    proxy_bars = [{"t": "2026-07-14T08:00:00Z",
                   "o": 750.0, "h": 753.9, "l": 748.1, "c": 752.0, "v": 500}]
    monkeypatch.setattr(overnight_context, "_fetch_alpaca_extended_hours",
                        lambda symbol, s, e: proxy_bars)

    bars, source = overnight_context._get_overnight_bars(TARGET, "ES")
    assert source == "etf_proxy"
    assert bars == proxy_bars


def test_overnight_range_carries_futures_source(monkeypatch):
    _no_databento(monkeypatch)
    monkeypatch.setattr(overnight_context, "_fetch_yahoo_futures",
                        lambda contract, s, e: _futures_bars())
    on = overnight_context.overnight_range(TARGET, "ES")
    assert on["source"] == "futures"
    assert on["high"] == 7611.0
    assert on["low"] == 7570.0


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
