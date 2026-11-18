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


def test_exchange_stop_payload_and_shadow_index_gear(make_policy, tmp_path) -> None:
    import pandas as pd

    from vito.exec import build_exchange_stop_payload, build_shadow_plan

    swap_stop = build_exchange_stop_payload(
        "QQQ-USDT-SWAP", wallet="future", qty=0.25, stop_price=640.5
    )
    assert swap_stop["type"] == "STOP_MARKET"
    assert swap_stop["params"]["reduceOnly"] is True
    assert swap_stop["params"]["workingType"] == "CONTRACT_PRICE"

    spot_stop = build_exchange_stop_payload(
        "PAXG-USDT", wallet="spot", qty=0.02, stop_price=4800.0
    )
    assert spot_stop["type"] == "STOP_LOSS_LIMIT"
    assert spot_stop["params"]["timeInForce"] == "GTC"

    pol = make_policy(execution__audit_log=str(tmp_path / "audit.jsonl"))

    class DummyGateway:
        def earn_info(self, asset: str = "USDT") -> dict:
            return {"asset": asset, "amount": 93.16, "apr_pct": 10.28}

        def min_notional(self, sym: str) -> float:
            return 5.0

    ts = [1_700_000_000_000 + i * 86_400_000 for i in range(40)]
    qqq_up = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [500.0 * (1.004 ** (i + (i % 2) * 0.05)) for i in range(40)],
            "high": [501.0 * (1.004 ** (i + (i % 2) * 0.05)) for i in range(40)],
            "low": [499.0 * (1.004 ** (i + (i % 2) * 0.05)) for i in range(40)],
            "close": [500.0 * (1.004 ** (i + (i % 2) * 0.05)) for i in range(40)],
            "volume": [1000.0] * 40,
        }
    )
    plan = build_shadow_plan(
        policy=pol,
        gateway=DummyGateway(),
        frames={"QQQ-USDT-SWAP": qqq_up},
        equity_usdt=93.16,
        cash_usdt=93.16,
        lookback=20,
    )
    assert len(plan.index_gear) == 1
    assert plan.index_gear[0].inst_id == "QQQ-USDT-SWAP"
    assert plan.index_gear[0].gear_mult == 3.0
    assert plan.orders[0].stop_order["type"] == "STOP_MARKET"
    assert plan.orders[0].shadow_geared_notional_usdt == pytest.approx(
        plan.orders[0].target_notional_usdt * 3.0, abs=0.05
    )
