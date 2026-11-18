from __future__ import annotations

import pytest

from vito.exec import ExecutionLocked, OrderRouter


def test_router_refuses_orders_until_armed(policy) -> None:
    router = OrderRouter(policy)
    assert not router.armed
    with pytest.raises(ExecutionLocked):
        router.place({"side": "long"})


def test_router_cannot_arm_in_readonly_mode(policy) -> None:
    router = OrderRouter(policy)
    with pytest.raises(ExecutionLocked, match="readonly"):
        router.arm(policy.digest())


def test_router_requires_digest_confirmation(make_policy, monkeypatch) -> None:
    live = make_policy(execution__mode="live")
    monkeypatch.setenv("VITO_LIVE", "1")
    router = OrderRouter(live)
    with pytest.raises(ExecutionLocked, match="digest mismatch"):
        router.arm("deadbeefdeadbeef")

    router.arm(live.digest())
    assert router.armed


def test_router_requires_live_env_flag(make_policy) -> None:
    live = make_policy(execution__mode="live")
    router = OrderRouter(live)
    with pytest.raises(ExecutionLocked, match="VITO_LIVE"):
        router.arm(live.digest())


def test_kill_switch_blocks_arming(make_policy, monkeypatch, tmp_path) -> None:
    halt = tmp_path / "HALT"
    halt.write_text("stop")
    live = make_policy(execution__mode="live", execution__kill_switch_file=str(halt))
    monkeypatch.setenv("VITO_LIVE", "1")
    with pytest.raises(ExecutionLocked, match="kill switch"):
        OrderRouter(live).arm(live.digest())


def test_build_shadow_plan_respects_floor_and_logs_audit(make_policy, tmp_path) -> None:
    import pandas as pd

    from vito.exec import build_shadow_plan
    from vito.exec.audit import AuditLog

    pol = make_policy(execution__audit_log=str(tmp_path / "audit.jsonl"))

    class DummyGateway:
        def earn_info(self, asset: str = "USDT") -> dict:
            return {"asset": asset, "amount": 93.16, "apr_pct": 10.25}

        def min_notional(self, sym: str) -> float:
            return 5.0

    ts = [1_700_000_000_000 + i * 86_400_000 for i in range(30)]
    up = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [100.0 * (1.01 ** (i + (i % 3) * 0.1)) for i in range(30)],
            "high": [101.0 * (1.01 ** (i + (i % 3) * 0.1)) for i in range(30)],
            "low": [99.0 * (1.01 ** (i + (i % 3) * 0.1)) for i in range(30)],
            "close": [100.0 * (1.01 ** (i + (i % 3) * 0.1)) for i in range(30)],
            "volume": [1000.0] * 30,
        }
    )

    plan = build_shadow_plan(
        policy=pol,
        gateway=DummyGateway(),
        frames={"PAXG-USDT": up},
        equity_usdt=93.16,
        cash_usdt=93.16,
        lookback=15,
    )
    assert plan.below_floor is False
    assert len(plan.orders) == 1
    assert plan.orders[0].executable is True
    assert plan.audit_hash != ""
    assert AuditLog(tmp_path / "audit.jsonl").verify() is True

    # Below $50 floor -> refuses all orders
    floored = build_shadow_plan(
        policy=pol,
        gateway=DummyGateway(),
        frames={"PAXG-USDT": up},
        equity_usdt=45.0,
        cash_usdt=45.0,
        lookback=15,
    )
    assert floored.below_floor is True
    assert len(floored.orders) == 0
