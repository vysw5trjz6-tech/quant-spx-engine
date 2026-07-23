"""Databento availability/licensing-horizon handling.

Two rejections were the constant "databento data issues" in the logs:

  * 422 data_end_after_available_end -- `end` reaches past the dataset's
    published range (e.g. EQUS.MINI queried with tomorrow's date to capture
    today's not-yet-settled bar).
  * 403 license_not_found_unauthorized -- the window reaches into the recent
    period Databento only serves to LIVE-licensed accounts ("A live data
    license is required to access OPRA.PILLAR data after <cutoff>").

Neither is an account-state problem: the fix is to clamp `end` to the
server-reported cutoff and retry, NOT to fail the fetch and (for the 403)
trip the shared 30-minute billing breaker that blanks OI/GEX/IV.
"""
import os
import sys
from datetime import datetime

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import databento_adapter as dba


_EQ_422 = (
    "422 data_end_after_available_end The dataset EQUS.MINI has data "
    "available up to '2026-07-21 00:00:00+00:00'. The `end` in the query "
    "('2026-07-22 00:00:00+00:00') is after the available range."
)
_OPRA_403 = (
    "403 license_not_found_unauthorized A live data license is required to "
    "access OPRA.PILLAR data after 2026-07-20T13:30:00.000000000Z."
)
# GLBX/CME licensed datasets phrase the licensing-horizon rejection as a 422
# dataset_unavailable_range with a "Try again with an end time before" hint.
_GLBX_422 = (
    "422 dataset_unavailable_range Part or all of your request for dataset "
    "'GLBX.MDP3' requires a subscription and/or license to access. Try again "
    "with an end time before 2026-07-22T05:13:44.388091000Z."
)


def _reset():
    dba._billing_blocked_until = None
    dba._avail_horizon.clear()


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

def test_422_window_error_is_not_billing():
    # A 422 out-of-range window must never open the shared breaker.
    assert dba._is_window_error(Exception(_EQ_422))
    assert not dba._is_billing_error(Exception(_EQ_422))


def test_403_license_is_window_not_billing():
    # The OPRA "live data license required" 403 is a licensing-horizon
    # problem, NOT account-locked. It used to match the blanket 403 test in
    # _is_billing_error and trip the breaker; it must not any more.
    assert dba._is_window_error(Exception(_OPRA_403))
    assert not dba._is_billing_error(Exception(_OPRA_403))


def test_real_403_account_lock_still_billing():
    # A genuine account lock (no licensing wording) is still billing.
    assert dba._is_billing_error(Exception("403 auth_account_locked"))
    assert not dba._is_window_error(Exception("403 auth_account_locked"))


# ---------------------------------------------------------------------------
# Cutoff parsing
# ---------------------------------------------------------------------------

def test_cutoff_parsed_from_422():
    cutoff = dba._availability_cutoff(Exception(_EQ_422))
    assert cutoff == datetime(2026, 7, 21, 0, 0, 0)


def test_cutoff_parsed_from_403():
    cutoff = dba._availability_cutoff(Exception(_OPRA_403))
    assert cutoff == datetime(2026, 7, 20, 13, 30, 0)


def test_glbx_422_is_window_not_billing():
    # The GLBX 422 dataset_unavailable_range is a licensing-horizon problem,
    # not an account-state one: it must never trip the shared breaker.
    assert dba._is_window_error(Exception(_GLBX_422))
    assert not dba._is_billing_error(Exception(_GLBX_422))


def test_cutoff_parsed_from_glbx_422():
    cutoff = dba._availability_cutoff(Exception(_GLBX_422))
    assert cutoff == datetime(2026, 7, 22, 5, 13, 44)


# ---------------------------------------------------------------------------
# Overnight bars defer to the fallback rather than clamp to a truncated session
# ---------------------------------------------------------------------------

def test_overnight_defers_when_horizon_misses_window(monkeypatch):
    # When GLBX's readable range ends before the overnight window closes, the
    # overnight pull must be skipped (so the full-session fallback runs) and
    # NOT issued — a clamped, truncated session would be a silently wrong
    # overnight high/low.
    from datetime import date

    _reset()
    # Learn a horizon well inside the window (window ends target 14:30 UTC).
    dba._learn_horizon("GLBX.MDP3", datetime(2026, 7, 22, 5, 13, 44))

    class _NeverTimeseries:
        def get_range(self, **k):
            raise AssertionError("overnight pull should not be issued")

    class _Client:
        timeseries = _NeverTimeseries()

    monkeypatch.setattr(dba, "_get_client", lambda: _Client())
    monkeypatch.setattr(dba, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(dba, "_neg_cached", lambda *a, **k: False)
    monkeypatch.setattr(dba, "_neg_cache_set", lambda *a, **k: None)

    bars = dba.get_overnight_bars("ES", date(2026, 7, 22))
    assert bars == []
    assert not dba._billing_blocked()
    _reset()


def test_overnight_pulls_when_horizon_covers_window(monkeypatch):
    # When the readable range covers the whole window, the pull proceeds.
    from datetime import date

    _reset()
    # Horizon comfortably past the window end (target 14:30 UTC).
    dba._learn_horizon("GLBX.MDP3", datetime(2026, 7, 23, 0, 0, 0))

    class _Row(dict):
        pass

    class _DFBars:
        empty = False

        def iterrows(self):
            row = {"open": 5000.0, "high": 5010.0, "low": 4990.0,
                   "close": 5005.0, "volume": 100}
            return iter([(datetime(2026, 7, 22, 3, 0, 0), row)])

    class _Timeseries:
        def __init__(self):
            self.called = 0

        def get_range(self, **k):
            self.called += 1

            class _R:
                def to_df(_self):
                    return _DFBars()
            return _R()

    ts = _Timeseries()

    class _Client:
        timeseries = ts

    monkeypatch.setattr(dba, "_get_client", lambda: _Client())
    monkeypatch.setattr(dba, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(dba, "_neg_cached", lambda *a, **k: False)
    monkeypatch.setattr(dba, "_cache_set", lambda *a, **k: None)
    monkeypatch.setattr(dba, "_neg_cache_set", lambda *a, **k: None)

    bars = dba.get_overnight_bars("ES", date(2026, 7, 22))
    assert ts.called == 1
    assert len(bars) == 1 and bars[0]["h"] == 5010.0
    _reset()


# ---------------------------------------------------------------------------
# Clamp-and-retry via _get_range_clamped
# ---------------------------------------------------------------------------

class _DF:
    """Minimal stand-in for a non-empty DataFrame."""
    empty = False


class _RejectOnceClient:
    """Rejects the first get_range with `exc`, then records the retry's
    `end` and returns a non-empty frame."""
    def __init__(self, exc):
        self.calls = []
        outer = self

        class _TS:
            def get_range(_self, **k):
                outer.calls.append(k["end"])
                if len(outer.calls) == 1:
                    raise exc

                class _R:
                    def to_df(self_inner):
                        return _DF()
                return _R()
        self.timeseries = _TS()


def test_clamp_and_retry_on_422():
    _reset()
    client = _RejectOnceClient(Exception(_EQ_422))
    df = dba._get_range_clamped(
        client, "EQUS.MINI", "2026-06-01", "2026-07-22",
        symbols=["SPY"], stype_in="raw_symbol", schema="ohlcv-1d")
    assert df is not None and not df.empty
    # Retried with end clamped to the reported cutoff.
    assert client.calls[0] == "2026-07-22"
    assert client.calls[1] == "2026-07-21T00:00:00"
    # Horizon learned for preemptive clamping next time.
    assert dba._horizon_end("EQUS.MINI") == datetime(2026, 7, 21, 0, 0, 0)
    _reset()


def test_preemptive_clamp_uses_learned_horizon():
    _reset()
    dba._learn_horizon("EQUS.MINI", datetime(2026, 7, 21, 0, 0, 0))

    class _RecordClient:
        def __init__(self):
            self.calls = []
            outer = self

            class _TS:
                def get_range(_self, **k):
                    outer.calls.append(k["end"])

                    class _R:
                        def to_df(self_inner):
                            return _DF()
                    return _R()
            self.timeseries = _TS()

    client = _RecordClient()
    dba._get_range_clamped(
        client, "EQUS.MINI", "2026-06-01", "2026-07-22",
        symbols=["SPY"], stype_in="raw_symbol", schema="ohlcv-1d")
    # No rejected probe first: the single call already used the clamped end.
    assert client.calls == ["2026-07-21T00:00:00"]
    _reset()


def test_empty_window_returns_none_without_call():
    # If the whole window sits past the horizon, don't even issue the call.
    _reset()
    dba._learn_horizon("OPRA.PILLAR", datetime(2026, 7, 20, 13, 30, 0))

    class _NeverClient:
        def __init__(self):
            self.calls = 0
            outer = self

            class _TS:
                def get_range(_self, **k):
                    outer.calls += 1
                    raise AssertionError("should not be called")
            self.timeseries = _TS()

    client = _NeverClient()
    out = dba._get_range_clamped(
        client, "OPRA.PILLAR", "2026-07-21", "2026-07-22",
        symbols=["SPX.OPT"], stype_in="parent", schema="statistics")
    assert out is None
    assert client.calls == 0
    _reset()


def test_non_window_error_propagates():
    _reset()

    class _Boom:
        def __init__(self):
            outer = self

            class _TS:
                def get_range(_self, **k):
                    raise Exception("500 internal server error")
            self.timeseries = _TS()

    import pytest
    with pytest.raises(Exception):
        dba._get_range_clamped(
            _Boom(), "EQUS.MINI", "2026-06-01", "2026-06-10",
            symbols=["SPY"], stype_in="raw_symbol", schema="ohlcv-1d")
    _reset()


def test_equity_422_does_not_trip_breaker(monkeypatch):
    # End-to-end: get_equity_bars against a client that 422s once then
    # succeeds returns bars and leaves the breaker closed.
    _reset()
    client = _RejectOnceClient(Exception(_EQ_422))

    # _DF here needs to look like a real (non-empty) frame get_equity_bars
    # can iterate; give it a trivial iterrows returning nothing so the
    # function returns [] but WITHOUT tripping the breaker or emitting a
    # fetch_failed.
    class _EmptyIter(_DF):
        empty = False

        def iterrows(self):
            return iter(())

    class _OneShot(_RejectOnceClient):
        def __init__(self, exc):
            super().__init__(exc)
            outer = self

            class _TS:
                def get_range(_self, **k):
                    outer.calls.append(k["end"])
                    if len(outer.calls) == 1:
                        raise exc

                    class _R:
                        def to_df(self_inner):
                            return _EmptyIter()
                    return _R()
            self.timeseries = _TS()

    client = _OneShot(Exception(_EQ_422))
    monkeypatch.setattr(dba, "_get_client", lambda: client)
    monkeypatch.setattr(dba, "_neg_cached", lambda *a, **k: False)
    monkeypatch.setattr(dba, "_neg_cache_set", lambda *a, **k: None)

    bars = dba.get_equity_bars("SPY", "2026-06-01", "2026-07-22")
    assert bars == []
    assert not dba._billing_blocked()
    assert len(client.calls) == 2  # clamped retry happened
    _reset()
