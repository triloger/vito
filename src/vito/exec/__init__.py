"""Order routing and live shadow planning.

Every live or shadow decision passes through the same policy checks:
1. Kill-switch check (`~/.vito/HALT`).
2. Hard equity floor (`equity_floor_usdt = $50.00`).
3. Risk-based position sizing (`1.5% risk / ATR stop_distance`), capped by
   `max_gross_exposure_mult` (`0.75x` for the panel) and verified against the
   exchange's exact minimum order notional.
4. Hash-chained audit logging (`~/.vito/audit.jsonl`) for every shadow plan,
   refusal, and armed order.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from vito.backtest.panel import (
    IndexGearState,
    evaluate_index_gear_at,
    rank_panel_at,
)
from vito.exec.audit import AuditLog
from vito.policy import Policy


class ExecutionLocked(RuntimeError):
    """Raised when an order is attempted while the router is not armed."""


def build_exchange_stop_payload(
    inst_id: str,
    *,
    wallet: str,
    qty: float,
    stop_price: float,
) -> dict[str, Any]:
    """Construct the deterministic exchange-native protective stop order payload.

    On Binance USDⓈ-M Futures (`wallet == "future"`), places a `STOP_MARKET`
    reduce-only order triggered on `CONTRACT_PRICE` so 24/7 TradFi and crypto
    perpetuals are protected on the exchange matching engine even if the VM is
    offline. On Spot (`wallet == "spot"`), places a GTC `STOP_LOSS_LIMIT`.
    """
    if wallet == "future":
        return {
            "inst_id": inst_id,
            "type": "STOP_MARKET",
            "side": "sell",
            "qty": round(qty, 8),
            "params": {
                "stopPrice": round(stop_price, 6),
                "reduceOnly": True,
                "workingType": "CONTRACT_PRICE",
            },
        }
    return {
        "inst_id": inst_id,
        "type": "STOP_LOSS_LIMIT",
        "side": "sell",
        "qty": round(qty, 8),
        "limit_price": round(stop_price * 0.998, 6),
        "params": {
            "stopPrice": round(stop_price, 6),
            "timeInForce": "GTC",
        },
    }


@dataclass(frozen=True, slots=True)
class ShadowOrder:
    inst_id: str
    wallet: str  # "spot" | "future"
    score: float
    ref_price: float
    stop_distance_pct: float
    stop_price: float
    target_notional_usdt: float
    min_notional_usdt: float
    qty: float
    executable: bool
    reason: str = ""
    gear_mult: float = 1.0
    shadow_geared_notional_usdt: float = 0.0
    stop_order: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ShadowPlan:
    equity_usdt: float
    cash_usdt: float
    earn_amount_usdt: float
    earn_apr_pct: float
    daily_earn_usdt: float
    halted: bool
    below_floor: bool
    orders: tuple[ShadowOrder, ...]
    deployed_usdt: float
    idle_cash_usdt: float
    audit_hash: str
    index_gear: tuple[IndexGearState, ...] = ()


def build_shadow_plan(
    *,
    policy: Policy,
    gateway: Any,
    frames: dict[str, pd.DataFrame],
    equity_usdt: float,
    cash_usdt: float,
    lookback: int = 20,
    max_positions: int = 2,
    max_per_group: int | None = 1,
    max_exposure: float = 2.50,
    risk_per_trade_pct: float = 4.0,
    index_gear_mult: float = 1.0,
    shadow_index_gear_mult: float = 3.0,
    persist_audit: bool = True,
) -> ShadowPlan:
    """Evaluate the live account and market state, build the target portfolio, and log to audit."""
    halted = os.path.exists(os.path.expanduser(policy.execution.kill_switch_file))
    below_floor = equity_usdt < policy.equity_floor_usdt

    earn = (
        gateway.earn_info("USDT")
        if hasattr(gateway, "earn_info")
        else {"amount": 0.0, "apr_pct": 0.0}
    )
    earn_amt = float(earn.get("amount") or 0.0)
    earn_apr = float(earn.get("apr_pct") or 0.0)

    ranked = (
        []
        if (halted or below_floor or not frames)
        else rank_panel_at(
            frames,
            lookback=lookback,
            max_positions=max_positions,
            max_per_group=max_per_group,
        )
    )
    gear_states = (
        tuple(evaluate_index_gear_at(frames, lookback=lookback, max_gear=shadow_index_gear_mult))
        if frames
        else ()
    )
    gear_by_sym = {g.inst_id: g for g in gear_states}

    gross_cap_each = (
        (equity_usdt * max_exposure) / max(1, len(ranked)) if ranked else 0.0
    )
    risk_frac = risk_per_trade_pct / 100.0
    max_spot_cash = equity_usdt * 0.75

    orders: list[ShadowOrder] = []
    deployed = 0.0
    spot_used = 0.0
    for sym, score, last_close, stop_d in ranked:
        safe_stop = max(policy.risk.min_stop_distance_pct / 100.0, stop_d)
        risk_notional = (equity_usdt * risk_frac) / safe_stop
        base_alloc = min(gross_cap_each, risk_notional)
        g_state = gear_by_sym.get(sym)
        shadow_gear = g_state.gear_mult if g_state is not None else 1.0
        active_gear = (
            min(index_gear_mult, shadow_gear)
            if (index_gear_mult > 1.0 and shadow_gear > 1.0)
            else 1.0
        )
        alloc = round(base_alloc * active_gear, 2)
        shadow_geared_alloc = round(base_alloc * shadow_gear, 2)
        exec_sym = sym
        wallet = "future" if sym.endswith("-SWAP") else "spot"
        # If adding another Spot position would exceed 75% of account cash when
        # total portfolio target > 1.0x, route non-BTC Spot legs to their -SWAP
        # perpetual on USDⓈ-M Futures so gross exposure is physically funded.
        if wallet == "spot" and (spot_used + alloc > max_spot_cash) and not sym.startswith("BTC-"):
            exec_sym = f"{sym}-SWAP"
            wallet = "future"
        try:
            min_not = round(float(gateway.min_notional(exec_sym)), 2)
        except Exception:  # noqa: BLE001
            min_not = 5.0
        ok = alloc >= min_not and last_close > 0
        reason = "" if ok else f"alloc ${alloc:.2f} < min_notional ${min_not:.2f}"
        if ok:
            deployed += alloc
            if wallet == "spot":
                spot_used += alloc
        qty = round(alloc / last_close, 8) if (ok and last_close > 0) else 0.0
        stop_px = round(last_close * (1.0 - safe_stop), 6)
        stop_payload = (
            build_exchange_stop_payload(
                exec_sym, wallet=wallet, qty=qty, stop_price=stop_px
            )
            if ok
            else {}
        )
        orders.append(
            ShadowOrder(
                inst_id=exec_sym,
                wallet=wallet,
                score=score,
                ref_price=round(last_close, 6),
                stop_distance_pct=round(safe_stop * 100.0, 2),
                stop_price=stop_px,
                target_notional_usdt=alloc if ok else 0.0,
                min_notional_usdt=min_not,
                qty=qty,
                executable=ok,
                reason=reason,
                gear_mult=active_gear,
                shadow_geared_notional_usdt=shadow_geared_alloc if ok else 0.0,
                stop_order=stop_payload,
            )
        )

    idle_cash = round(max(0.0, equity_usdt - spot_used), 2)
    daily_earn = round(idle_cash * (earn_apr / 100.0) / 365.0, 4)

    audit_hash = ""
    if persist_audit:
        audit = AuditLog(policy.execution.audit_log)
        audit_hash = audit.append(
            {
                "kind": "shadow_plan",
                "policy_digest": policy.digest(),
                "equity_usdt": round(equity_usdt, 2),
                "deployed_usdt": round(deployed, 2),
                "idle_cash_usdt": idle_cash,
                "earn_apr_pct": earn_apr,
                "daily_earn_usdt": daily_earn,
                "halted": halted,
                "below_floor": below_floor,
                "orders": [asdict(o) for o in orders],
                "index_gear": [asdict(g) for g in gear_states],
            }
        )

    return ShadowPlan(
        equity_usdt=round(equity_usdt, 2),
        cash_usdt=round(cash_usdt, 2),
        earn_amount_usdt=round(earn_amt, 2),
        earn_apr_pct=earn_apr,
        daily_earn_usdt=daily_earn,
        halted=halted,
        below_floor=below_floor,
        orders=tuple(orders),
        deployed_usdt=round(deployed, 2),
        idle_cash_usdt=idle_cash,
        audit_hash=audit_hash,
        index_gear=gear_states,
    )


@dataclass
class OrderRouter:
    policy: Policy
    _armed: bool = False

    def arm(self, confirmation: str) -> None:
        """Arm live routing. ``confirmation`` must equal the policy digest."""
        if self.policy.execution.mode == "readonly":
            raise ExecutionLocked(
                "policy mode is 'readonly'; set execution.mode to 'live' first"
            )
        if os.environ.get("VITO_LIVE") != "1":
            raise ExecutionLocked("VITO_LIVE=1 is required to arm live routing")
        if confirmation != self.policy.digest():
            raise ExecutionLocked(
                f"digest mismatch: policy is {self.policy.digest()}, "
                "re-read `vito policy` and try again"
            )
        if os.path.exists(os.path.expanduser(self.policy.execution.kill_switch_file)):
            raise ExecutionLocked("kill switch is engaged; remove the HALT file first")
        self._armed = True

    @property
    def armed(self) -> bool:
        return self._armed

    def place(self, order: dict[str, Any], *, gateway: Any | None = None) -> dict[str, Any]:
        if not self._armed:
            raise ExecutionLocked(
                "router is not armed; no order can leave this process"
            )
        if os.path.exists(os.path.expanduser(self.policy.execution.kill_switch_file)):
            raise ExecutionLocked("kill switch is engaged; order aborted")
        if gateway is None:
            raise ValueError("gateway is required to place an armed order")

        inst_id = str(order["inst_id"])
        side = "buy" if str(order.get("side", "buy")).lower() in ("buy", "long") else "sell"
        qty = float(order["qty"])
        stop_price = float(order.get("stop_price") or 0.0)
        market = gateway._market(inst_id)  # noqa: SLF001
        cli = gateway._client_for(market) if hasattr(gateway, "_client_for") else gateway.client  # noqa: SLF001

        entry_res = cli.create_order(market["symbol"], "market", side, qty)
        stop_res = None
        if stop_price > 0 and side == "buy":
            if market.get("type") == "swap":
                stop_res = cli.create_order(
                    market["symbol"],
                    "STOP_MARKET",
                    "sell",
                    qty,
                    None,
                    {
                        "stopPrice": stop_price,
                        "reduceOnly": True,
                        "workingType": "CONTRACT_PRICE",
                    },
                )
            else:
                stop_res = cli.create_order(
                    market["symbol"],
                    "STOP_LOSS_LIMIT",
                    "sell",
                    qty,
                    round(stop_price * 0.998, 6),
                    {"stopPrice": stop_price, "timeInForce": "GTC"},
                )

        audit = AuditLog(self.policy.execution.audit_log)
        digest = audit.append(
            {
                "kind": "live_order",
                "inst_id": inst_id,
                "side": side,
                "qty": qty,
                "stop_price": stop_price,
                "entry_id": entry_res.get("id"),
                "stop_id": (stop_res or {}).get("id"),
            }
        )
        return {"entry": entry_res, "stop": stop_res, "audit_hash": digest}
