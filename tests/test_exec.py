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
