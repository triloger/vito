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


def test_7_stage_execution_runbook_and_live_router_lifecycle(
    make_policy, monkeypatch, tmp_path
) -> None:
    import pandas as pd

    from vito.exec import OrderRouter, build_execution_runbook, build_shadow_plan
    from vito.exec.audit import AuditLog

    audit_file = tmp_path / "audit.jsonl"
    pol = make_policy(execution__mode="live", execution__audit_log=str(audit_file))
    monkeypatch.setenv("VITO_LIVE", "1")

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def create_order(self, symbol, otype, side, qty, price=None, params=None):
            self.calls.append(("create_order", symbol, otype, side, qty, price, params))
            return {"id": f"ord-{len(self.calls)}"}

        def cancel_order(self, oid, symbol):
            self.calls.append(("cancel_order", oid, symbol))
            return {"id": oid, "status": "canceled"}

    class FakeGateway:
        def __init__(self) -> None:
            self.cli = FakeClient()
            self.breakdown = {
                "spot_free": 0.0,
                "future_free": 0.0,
                "earn_flexible": 93.16,
                "product_id": "USDT001",
                "earn_apr_pct": 10.22,
            }
            self.live_pos: dict = {}
            self.ops: list[tuple] = []

        def earn_info(self, asset: str = "USDT") -> dict:
            return {
                "asset": asset,
                "amount": self.breakdown["earn_flexible"],
                "apr_pct": 10.22,
                "product_id": "USDT001",
            }

        def min_notional(self, sym: str) -> float:
            return 5.0

        def round_order(self, inst_id: str, qty: float, price: float) -> tuple[float, float]:
            if inst_id == "QQQ-USDT-SWAP":
                return round(qty, 2), round(price, 2)
            return round(qty, 4), round(price, 2)

        def wallet_usdt_breakdown(self) -> dict:
            return dict(self.breakdown)

        def live_positions_and_stops(self, symbols) -> dict:
            return dict(self.live_pos)

        def _market(self, inst_id: str) -> dict:
            is_swap = inst_id.endswith("-SWAP")
            base = inst_id.split("-")[0]
            return {
                "symbol": f"{base}/USDT:USDT" if is_swap else f"{base}/USDT",
                "type": "swap" if is_swap else "spot",
            }

        def _client_for(self, market: dict) -> FakeClient:
            return self.cli

        def redeem_earn(self, amount_usdt: float, *, product_id: str, dest_account: str):
            self.ops.append(("redeem_earn", amount_usdt, product_id, dest_account))
            return {"success": True}

        def subscribe_earn(self, amount_usdt: float, *, product_id: str):
            self.ops.append(("subscribe_earn", amount_usdt, product_id))
            return {"success": True}

        def transfer_usdt(self, amount_usdt: float, *, from_wallet: str, to_wallet: str):
            self.ops.append(("transfer_usdt", amount_usdt, from_wallet, to_wallet))
            return {"tranId": 123}

    gw = FakeGateway()
    ts = [1_700_000_000_000 + i * 86_400_000 for i in range(40)]
    paxg_up = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [3800.0 * (1.006 ** (i + (i % 2) * 0.05)) for i in range(40)],
            "high": [3810.0 * (1.006 ** (i + (i % 2) * 0.05)) for i in range(40)],
            "low": [3790.0 * (1.006 ** (i + (i % 2) * 0.05)) for i in range(40)],
            "close": [3800.0 * (1.006 ** (i + (i % 2) * 0.05)) for i in range(40)],
            "volume": [1000.0] * 40,
        }
    )
    qqq_up = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [600.0 * (1.004 ** (i + (i % 2) * 0.05)) for i in range(40)],
            "high": [602.0 * (1.004 ** (i + (i % 2) * 0.05)) for i in range(40)],
            "low": [598.0 * (1.004 ** (i + (i % 2) * 0.05)) for i in range(40)],
            "close": [600.0 * (1.004 ** (i + (i % 2) * 0.05)) for i in range(40)],
            "volume": [1000.0] * 40,
        }
    )

    # 1. From 100% Simple Earn -> enter PAXG-USDT (Spot) + QQQ-USDT-SWAP (Futures)
    plan = build_shadow_plan(
        policy=pol,
        gateway=gw,
        frames={"PAXG-USDT": paxg_up, "QQQ-USDT-SWAP": qqq_up},
        equity_usdt=93.16,
        cash_usdt=93.16,
        lookback=20,
    )
    rb = build_execution_runbook(
        policy=pol,
        gateway=gw,
        plan=plan,
        symbols=("PAXG-USDT", "QQQ-USDT-SWAP"),
    )
    actions = [s.action for s in rb.steps]
    assert "REDEEM_EARN" in actions
    assert "TRANSFER" in actions
    assert actions.count("ENTRY_ORDER") == 2
    assert actions.count("PLACE_STOP") == 2

    router = OrderRouter(pol)
    router.arm(pol.digest())
    res = router.execute_runbook(rb, gateway=gw)
    assert res["steps_executed"] == len(rb.steps)
    assert any(op[0] == "redeem_earn" for op in gw.ops)
    assert any(op[0] == "transfer_usdt" and op[2] == "spot" and op[3] == "future" for op in gw.ops)

    # 2. Next day: PAXG-USDT exits, QQQ-USDT-SWAP ratchets stop & subscribes Earn
    qqq_order = next(o for o in plan.orders if o.inst_id == "QQQ-USDT-SWAP")
    gw.breakdown = {
        "spot_free": 2.0,
        "future_free": 40.0,
        "earn_flexible": 0.0,
        "product_id": "USDT001",
        "earn_apr_pct": 10.22,
    }
    gw.live_pos = {
        "PAXG-USDT": {
            "inst_id": "PAXG-USDT",
            "wallet": "spot",
            "qty": 0.012,
            "notional_usdt": 55.0,
            "stop_price": 4500.0,
            "stop_order_id": "stop-paxg-1",
        },
        "QQQ-USDT-SWAP": {
            "inst_id": "QQQ-USDT-SWAP",
            "wallet": "future",
            "qty": qqq_order.qty,
            "notional_usdt": qqq_order.target_notional_usdt,
            "stop_price": qqq_order.stop_price - 10.0,  # lower stop -> must ratchet up
            "stop_order_id": "stop-qqq-1",
        },
    }
    plan_day2 = build_shadow_plan(
        policy=pol,
        gateway=gw,
        frames={"QQQ-USDT-SWAP": qqq_up},
        equity_usdt=93.16,
        cash_usdt=42.0,
        lookback=20,
        max_exposure=1.25,
    )
    rb2 = build_execution_runbook(
        policy=pol,
        gateway=gw,
        plan=plan_day2,
        symbols=("PAXG-USDT", "QQQ-USDT-SWAP"),
    )
    act2 = [(s.stage, s.action, s.inst_id) for s in rb2.steps]
    # Stage 1 cancels stop and closes PAXG-USDT
    assert (1, "CANCEL_STOP", "PAXG-USDT") in act2
    assert (1, "CLOSE_POSITION", "PAXG-USDT") in act2
    # Stage 6 ratchets QQQ-USDT-SWAP stop higher without re-entering QQQ
    assert (6, "CANCEL_STOP", "QQQ-USDT-SWAP") in act2
    assert (6, "PLACE_STOP", "QQQ-USDT-SWAP") in act2
    # Stage 7 subscribes freed Spot cash back into Simple Earn
    assert (7, "SUBSCRIBE_EARN", "USDT") in act2
    assert AuditLog(audit_file).verify() is True
