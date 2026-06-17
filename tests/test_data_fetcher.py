import data_fetcher
import volume_truth


def _fake_bars(symbol, timeframe, limit, **kwargs):
    _fake_bars.calls += 1
    return [{"o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100, "t": "2026-01-01T14:30:00Z"}
            for _ in range(limit)]
_fake_bars.calls = 0


def test_intraday_caches_repeat_calls(monkeypatch):
    data_fetcher.clear_caches()
    _fake_bars.calls = 0
    monkeypatch.setattr(data_fetcher, "_fetch_bars", _fake_bars)

    a = data_fetcher.get_intraday("FAKE")
    b = data_fetcher.get_intraday("FAKE")
    c = data_fetcher.get_intraday("FAKE")
    assert a is not None and a == b == c
    # Should only have hit the network once.
    assert _fake_bars.calls == 1


def test_distinct_symbols_each_fetch_once(monkeypatch):
    data_fetcher.clear_caches()
    _fake_bars.calls = 0
    monkeypatch.setattr(data_fetcher, "_fetch_bars", _fake_bars)

    data_fetcher.get_intraday("AAA")
    data_fetcher.get_intraday("BBB")
    data_fetcher.get_intraday("AAA")  # cache hit
    assert _fake_bars.calls == 2


def test_4hr_groups_bars_into_fours(monkeypatch):
    data_fetcher.clear_caches()
    monkeypatch.setattr(data_fetcher, "_fetch_bars", _fake_bars)

    grouped = data_fetcher.get_4hr_bars("FAKE")
    # 80 1hr bars in -> 20 grouped 4hr bars (80 // 4)
    assert grouped is not None
    assert len(grouped) == 20
    for g in grouped:
        assert {"o", "h", "l", "c", "v", "t"} <= g.keys()


def test_prefetch_warms_all_caches(monkeypatch):
    data_fetcher.clear_caches()
    _fake_bars.calls = 0
    monkeypatch.setattr(data_fetcher, "_fetch_bars", _fake_bars)

    out = data_fetcher.prefetch_symbols(["AAA", "BBB"])
    assert set(out.keys()) == {"AAA", "BBB"}
    for sym in ("AAA", "BBB"):
        assert set(out[sym].keys()) == {"intraday", "daily", "1hr", "4hr"}

    # 2 symbols * 4 series = 8 fetches
    assert _fake_bars.calls == 8

    # Subsequent reads should hit cache (no new network calls).
    before = _fake_bars.calls
    data_fetcher.get_intraday("AAA")
    data_fetcher.get_daily("BBB")
    assert _fake_bars.calls == before


def test_default_feed_is_iex():
    # Free Alpaca keys are IEX-only; the implicit SIP default returns empty
    # bars and starves every consumer. Default must be an entitled feed.
    assert data_fetcher.ALPACA_FEED == "iex"


def test_live_and_profile_feeds_match():
    # The profile medians are compared directly against live bars, so the two
    # modules MUST request the same feed or every volume ratio is garbage.
    assert data_fetcher.ALPACA_FEED == volume_truth.ALPACA_FEED


def test_fetch_bars_pins_feed(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200
        def json(self):
            return {"bars": []}

    def _fake_get(url, headers=None, params=None, timeout=None):
        captured["params"] = params
        return _Resp()

    monkeypatch.setattr(data_fetcher.requests, "get", _fake_get)
    data_fetcher._fetch_bars("FAKE", "5Min", 78, start="2026-01-01T09:30:00-05:00")
    assert captured["params"]["feed"] == data_fetcher.ALPACA_FEED
