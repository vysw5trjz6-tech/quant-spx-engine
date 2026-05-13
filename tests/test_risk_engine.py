import os

import risk_engine


def test_risk_percent_tiers():
    assert risk_engine.get_risk_percent(90) == 0.05
    assert risk_engine.get_risk_percent(85) == 0.05
    assert risk_engine.get_risk_percent(80) == 0.03
    assert risk_engine.get_risk_percent(75) == 0.03
    assert risk_engine.get_risk_percent(72) == 0.02
    assert risk_engine.get_risk_percent(69) == 0.0
    assert risk_engine.get_risk_percent(0)  == 0.0


def test_calculate_contracts_below_threshold_returns_zeros():
    contracts, stop, target = risk_engine.calculate_contracts(premium=2.50, score=60)
    assert (contracts, stop, target) == (0, 0, 0)


def test_calculate_contracts_sizing_matches_risk_budget():
    # 30k account x 3% = $900 risk budget at score 75.
    # max_loss_per_contract = premium * 100 * 0.45 = 2.0 * 45 = $90
    # contracts = 900 // 90 = 10
    contracts, stop, target = risk_engine.calculate_contracts(premium=2.0, score=75)
    assert contracts == 10
    assert stop   == round(2.0 * 0.55, 2)
    assert target == round(2.0 * 1.40, 2)


def test_account_size_env_override(monkeypatch):
    # Reload the module with a different ACCOUNT_SIZE env var to confirm
    # the env-var hookup actually takes effect.
    monkeypatch.setenv("ACCOUNT_SIZE", "60000")
    import importlib
    importlib.reload(risk_engine)
    assert risk_engine.ACCOUNT_SIZE == 60000

    contracts, _, _ = risk_engine.calculate_contracts(premium=2.0, score=75)
    # 60k * 3% = $1800 / $90 = 20
    assert contracts == 20

    # Restore for sibling tests
    monkeypatch.delenv("ACCOUNT_SIZE", raising=False)
    importlib.reload(risk_engine)
