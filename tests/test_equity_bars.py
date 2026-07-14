"""Tests for the Databento equity-bar adapter's error classification.

A wrong/unentitled equity dataset returns a 403 -- that must fall back to
Alpaca, NOT open the shared billing breaker (which would also take down the
funded options + futures paths). Only a genuine 402 insufficient-funds trips
the breaker.
"""
import databento_adapter as dba


def test_402_insufficient_funds_is_billing():
    assert dba._is_insufficient_funds(Exception("402 account_insufficient_funds"))
    assert dba._is_insufficient_funds(Exception("you have insufficient funds"))


def test_403_entitlement_is_not_billing():
    # 403 / not-entitled must NOT be treated as out-of-funds.
    assert not dba._is_insufficient_funds(Exception("403 auth_no_dataset_entitlement"))
    assert not dba._is_insufficient_funds(Exception("symbology resolution failed"))


def test_default_dataset_is_consolidated_equities():
    # Default targets the consolidated US-equities feed, env-overridable.
    assert dba.EQUITY_DATASET  # non-empty


class _RaisingClient:
    """Stand-in Databento client whose every range query raises `exc`."""
    def __init__(self, exc):
        self._exc = exc

        class _TS:
            def get_range(_self, *a, **k):
                raise exc
        self.timeseries = _TS()


def _reset_breaker():
    dba._billing_blocked_until = None


def test_vix_proxy_403_does_not_trip_shared_breaker(monkeypatch):
    # XCBF.PITCH 403 (no live-data license) is an entitlement problem on a
    # single non-critical proxy. It must NOT open the shared billing breaker,
    # which would blank the funded equity + options paths for 30 min.
    _reset_breaker()
    monkeypatch.setattr(
        dba, "_get_client",
        lambda: _RaisingClient(Exception("403 auth_account_locked: live license required")))
    monkeypatch.setattr(dba, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(dba, "_neg_cached", lambda *a, **k: False)
    monkeypatch.setattr(dba, "_neg_cache_set", lambda *a, **k: None)

    assert dba.get_vix_proxy() is None
    assert not dba._billing_blocked()   # breaker stays closed
    _reset_breaker()


def test_vix_proxy_402_does_trip_shared_breaker(monkeypatch):
    # A genuine out-of-funds 402 still trips the breaker.
    _reset_breaker()
    monkeypatch.setattr(
        dba, "_get_client",
        lambda: _RaisingClient(Exception("402 account_insufficient_funds")))
    monkeypatch.setattr(dba, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(dba, "_neg_cached", lambda *a, **k: False)
    monkeypatch.setattr(dba, "_neg_cache_set", lambda *a, **k: None)

    assert dba.get_vix_proxy() is None
    assert dba._billing_blocked()       # breaker opened
    _reset_breaker()


def test_billing_status_reports_last_trip_reason(monkeypatch):
    # Diagnostics must show WHY the breaker opened, not just that it did:
    # the root-cause stderr line rotates out of the debug ring, and
    # /databento used to report available=false with no explanation.
    _reset_breaker()
    monkeypatch.setattr(dba, "_last_billing_trip", None)
    monkeypatch.setattr(
        dba, "_get_client",
        lambda: _RaisingClient(Exception("402 account_insufficient_funds")))
    monkeypatch.setattr(dba, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(dba, "_neg_cached", lambda *a, **k: False)
    monkeypatch.setattr(dba, "_neg_cache_set", lambda *a, **k: None)

    dba.get_vix_proxy()
    status = dba.billing_status()
    assert status["blocked"]
    trip = status["last_trip"]
    assert trip["context"] == "vix proxy"
    assert "account_insufficient_funds" in trip["detail"]
    assert trip["at"]
    _reset_breaker()
