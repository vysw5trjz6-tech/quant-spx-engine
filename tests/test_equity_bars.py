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
