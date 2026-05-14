"""Tests for the price-freshness and today-only intraday filters.

These cover the three bugs found 2026-05-14:
  1. Premarket brief firing too early (8:30 ET) -- not unit-tested
     directly because it's a scheduler offset; covered by manual review.
  2. Intraday bars including yesterday's session, poisoning ORB/VWAP/gap.
  3. Stale IEX quotes producing wrong spot prices on individual stocks.
"""
import os
import sys
import types
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
import pytz

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# =============================================
# Fix 2: get_intraday must pass start=today_09:30_ET to Alpaca
# =============================================

def test_get_intraday_filters_to_today_session():
    """The bars fetch must include a `start` param at today's 9:30 ET so
    Alpaca doesn't bleed in yesterday's bars."""
    import data_fetcher

    captured = {}

    class _FakeResp:
        status_code = 200
        def json(self):
            return {"bars": [{"t": "x", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]}

    def _fake_get(url, headers=None, params=None, timeout=None):
        captured["url"]    = url
        captured["params"] = params
        return _FakeResp()

    data_fetcher._INTRADAY_CACHE.clear()
    with patch.object(data_fetcher.requests, "get", side_effect=_fake_get):
        data_fetcher.get_intraday("SPY")

    assert "start" in captured["params"], "must pass start= to Alpaca"
    start = captured["params"]["start"]
    # ISO timestamp at exactly 09:30 ET (any offset format)
    assert "T09:30:00" in start
    # Today's date prefix
    today_et = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
    assert start.startswith(today_et)


def test_get_intraday_caches_per_symbol():
    import data_fetcher
    data_fetcher._INTRADAY_CACHE.clear()

    calls = {"n": 0}
    class _FakeResp:
        status_code = 200
        def json(self):
            calls["n"] += 1
            return {"bars": [{"o": 100}]}

    with patch.object(data_fetcher.requests, "get",
                      return_value=_FakeResp()):
        a = data_fetcher.get_intraday("AAPL")
        b = data_fetcher.get_intraday("AAPL")  # cache hit -- no second HTTP
    assert a == b
    assert calls["n"] == 1


# =============================================
# Fix 3: stale-price detection
# =============================================

def _make_snapshot(trade_age_s=None, bar_age_s=None, quote_age_s=None,
                   trade_px=100.0, bar_px=100.0, quote_bp=99.5, quote_ap=100.5):
    """Build a fake Alpaca snapshot dict with timestamps `age_s` ago."""
    now = datetime.now(pytz.utc)
    def _ts(age):
        return (now - timedelta(seconds=age)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")
    snap = {}
    if trade_age_s is not None:
        snap["latestTrade"] = {"t": _ts(trade_age_s), "p": trade_px, "s": 100}
    if bar_age_s is not None:
        # Minute bar timestamps at OPEN; the helper subtracts 30s when
        # scoring age, so pass `bar_age_s + 30` to model "bar closed now".
        snap["minuteBar"] = {"t": _ts(bar_age_s), "o": bar_px,
                             "h": bar_px, "l": bar_px, "c": bar_px, "v": 100}
    if quote_age_s is not None:
        snap["latestQuote"] = {"t": _ts(quote_age_s),
                               "bp": quote_bp, "ap": quote_ap}
    return snap


def _patch_snapshot(monkeypatch, snap):
    class _R:
        status_code = 200
        def json(self): return snap
    import data_fetcher
    monkeypatch.setattr(data_fetcher.requests, "get",
                        lambda *a, **kw: _R())


def test_fresh_trade_wins(monkeypatch):
    import data_fetcher
    _patch_snapshot(monkeypatch, _make_snapshot(
        trade_age_s=2, trade_px=432.77,
        quote_age_s=5, quote_bp=432.70, quote_ap=432.80,
    ))
    price, age, source = data_fetcher.get_price_with_freshness("AVGO")
    assert source == "trade"
    assert price == 432.77
    assert age < 5


def test_stale_quote_detected(monkeypatch):
    """The original AVGO bug: a quote 600s old should be flagged stale."""
    import data_fetcher
    _patch_snapshot(monkeypatch, _make_snapshot(
        quote_age_s=600, quote_bp=414.0, quote_ap=415.0))
    price, age, source = data_fetcher.get_price_with_freshness("AVGO")
    assert source == "quote"
    assert age > data_fetcher.STALE_PRICE_MAX_AGE_SECONDS


def test_fresh_price_passes_freshness_gate(monkeypatch):
    """Test the freshness computation directly, decoupled from the RTH clock."""
    import data_fetcher
    _patch_snapshot(monkeypatch, _make_snapshot(
        trade_age_s=5, trade_px=432.77))
    price, age, source = data_fetcher.get_price_with_freshness("AVGO")
    assert source == "trade"
    assert price == 432.77
    assert age < data_fetcher.STALE_PRICE_MAX_AGE_SECONDS


def test_no_snapshot_data_is_stale(monkeypatch):
    """Empty snapshot -> treat as stale; never produce a signal."""
    import data_fetcher
    _patch_snapshot(monkeypatch, {})
    stale, age, source = data_fetcher.is_price_stale("AVGO")
    assert stale is True
    assert age is None
