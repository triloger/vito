from __future__ import annotations

import pytest

from vito.policy import Policy, PolicyError


def test_shipped_policy_is_valid(policy: Policy) -> None:
    assert policy.version == "1.1"
    assert policy.risk.risk_per_trade_pct == 1.5
    assert policy.risk.max_gross_exposure_mult == 3.0
    assert policy.execution.mode == "readonly"


def test_digest_is_stable_across_loads(policy: Policy) -> None:
    again = Policy.from_dict(policy.as_dict())
    assert policy.digest() == again.digest()
    assert len(policy.digest()) == 16


def test_digest_changes_when_a_cap_changes(make_policy) -> None:
    base = make_policy()
    looser = make_policy(risk__risk_per_trade_pct=1.6)
    assert base.digest() != looser.digest()


def test_conviction_tier_is_btc_only(policy: Policy) -> None:
    assert policy.risk_fraction(conviction=True, instrument="BTC-USDT-SWAP") == 0.02
    assert policy.risk_fraction(conviction=True, instrument="ETH-USDT-SWAP") == 0.015
    assert policy.risk_fraction() == 0.015


def test_rejects_risk_above_ceiling(make_policy) -> None:
    with pytest.raises(PolicyError, match="risk_per_trade_pct"):
        make_policy(risk__risk_per_trade_pct=9.0)


def test_rejects_leverage_above_ceiling(make_policy) -> None:
    with pytest.raises(PolicyError, match="max_leverage"):
        make_policy(risk__max_leverage=100.0)


def test_rejects_gross_exposure_above_ceiling(make_policy) -> None:
    with pytest.raises(PolicyError, match="max_gross_exposure_mult"):
        make_policy(risk__max_gross_exposure_mult=10.0)


def test_rejects_config_that_can_arm_itself(make_policy) -> None:
    with pytest.raises(PolicyError, match="allow_live_from_config"):
        make_policy(execution__allow_live_from_config=True)


def test_rejects_unknown_execution_mode(make_policy) -> None:
    with pytest.raises(PolicyError, match="execution.mode"):
        make_policy(execution__mode="yolo")


def test_rejects_daily_halt_looser_than_drawdown_stop(make_policy) -> None:
    with pytest.raises(PolicyError, match="daily_loss_halt_pct"):
        make_policy(limits__daily_loss_halt_pct=20.0)


def test_rejects_conviction_instrument_not_whitelisted(make_policy) -> None:
    with pytest.raises(PolicyError, match="conviction_allowed"):
        make_policy(instruments__conviction_allowed=["SOL-USDT-SWAP"])


def test_rejects_missing_section(make_policy) -> None:
    raw = make_policy().as_dict()
    del raw["limits"]
    with pytest.raises(PolicyError, match=r"\[limits\]"):
        Policy.from_dict(raw)


def test_rejects_unknown_key_in_section(policy: Policy) -> None:
    raw = policy.as_dict()
    raw["risk"]["max_leverage_2"] = 10
    with pytest.raises(PolicyError, match="unknown keys"):
        Policy.from_dict(raw)
