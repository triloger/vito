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

import contextlib
import json
import math
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from vito.backtest.panel import (
    IndexGearState,
    align_panel,
    evaluate_index_gear_at,
    rank_panel_at,
)
from vito.exec.audit import AuditLog
from vito.policy import Policy
from vito.risk.limits import daily_loss_halt, drawdown_halt, streak_cooldown


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
    latest_bar_ts: int = 0
    uptrend_symbols: tuple[str, ...] = ()
    symbol_stops: dict[str, tuple[float, float, float]] = field(default_factory=dict)


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

    latest_bar_ts = 0
    uptrend_symbols: tuple[str, ...] = ()
    symbol_stops: dict[str, tuple[float, float, float]] = {}
    if frames:
        with contextlib.suppress(Exception):
            ts_arr, _, _, _, closes_map, stop_dists_map = align_panel(frames)
            if len(ts_arr) > 0:
                latest_bar_ts = int(ts_arr[-1])
            min_stop_frac = policy.risk.min_stop_distance_pct / 100.0
            for sym_k, c_arr in closes_map.items():
                if len(c_arr) == 0:
                    continue
                c_last = float(c_arr[-1])
                if not math.isfinite(c_last) or c_last <= 0:
                    continue
                sd_raw = float(stop_dists_map[sym_k][-1])
                safe_sd = max(
                    min_stop_frac,
                    sd_raw if (math.isfinite(sd_raw) and sd_raw > 0) else 0.02,
                )
                raw_sp = c_last * (1.0 - safe_sd)
                if hasattr(gateway, "round_order"):
                    _, sp_val = gateway.round_order(sym_k, 0.0, raw_sp)
                else:
                    sp_val = round(raw_sp, 6)
                entry_tuple = (round(c_last, 6), round(safe_sd * 100.0, 2), sp_val)
                symbol_stops[sym_k] = entry_tuple
                if not sym_k.endswith("-SWAP") and not sym_k.startswith("BTC-"):
                    swap_k = f"{sym_k}-SWAP"
                    if hasattr(gateway, "round_order"):
                        with contextlib.suppress(Exception):
                            _, sp_swap = gateway.round_order(swap_k, 0.0, raw_sp)
                            symbol_stops[swap_k] = (
                                round(c_last, 6),
                                round(safe_sd * 100.0, 2),
                                sp_swap,
                            )
                    symbol_stops.setdefault(swap_k, entry_tuple)

            all_up = rank_panel_at(
                frames,
                lookback=lookback,
                max_positions=max(1, len(frames)),
                max_per_group=None,
            )
            up_set: list[str] = []
            for sym_u, _, _, _ in all_up:
                up_set.append(sym_u)
                if not sym_u.endswith("-SWAP") and not sym_u.startswith("BTC-"):
                    up_set.append(f"{sym_u}-SWAP")
            uptrend_symbols = tuple(up_set)

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
        raw_qty = alloc / last_close if last_close > 0 else 0.0
        raw_stop_px = last_close * (1.0 - safe_stop)
        if hasattr(gateway, "round_order") and raw_qty > 0:
            qty, stop_px = gateway.round_order(exec_sym, raw_qty, raw_stop_px)
            alloc = round(qty * last_close, 2)
        else:
            qty = round(raw_qty, 8)
            stop_px = round(raw_stop_px, 6)
        ok = alloc >= min_not and qty > 0 and last_close > 0
        reason = "" if ok else f"alloc ${alloc:.2f} < min_notional ${min_not:.2f}"
        if ok:
            deployed += alloc
            if wallet == "spot":
                spot_used += alloc
        else:
            qty = 0.0
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
        latest_bar_ts=latest_bar_ts,
        uptrend_symbols=uptrend_symbols,
        symbol_stops=symbol_stops,
    )


LIVE_STATE_VERSION = 1


@dataclass(slots=True)
class LiveRiskState:
    """Persistent portfolio-level circuit-breaker and rebalance-cadence state."""

    version: int = LIVE_STATE_VERSION
    peak_equity_usdt: float = 0.0
    drawdown_pct: float = 0.0
    drawdown_halted: bool = False
    equity_24h_ago_usdt: float = 0.0
    pnl_24h_usdt: float = 0.0
    daily_loss_pct: float = 0.0
    daily_loss_halted: bool = False
    consecutive_losses: int = 0
    last_loss_ts_ms: int = 0
    cooldown_until_ms: int = 0
    streak_cooldown_active: bool = False
    last_rebalance_bar_ts: int = 0
    bars_since_rebalance: int = 0
    rebalance_due: bool = True
    equity_samples: list[dict[str, float]] = field(default_factory=list)
    open_positions: dict[str, dict[str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LiveRiskState:
        if int(data.get("version", 0)) != LIVE_STATE_VERSION:
            return cls()
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


def default_live_state_path(policy: Policy) -> Path:
    return Path(policy.execution.audit_log).expanduser().with_name("live_state.json")


def load_live_risk_state(path: Path | str) -> LiveRiskState:
    p = Path(path).expanduser()
    if not p.exists():
        return LiveRiskState()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return LiveRiskState.from_dict(raw if isinstance(raw, dict) else {})
    except Exception:  # noqa: BLE001
        return LiveRiskState()


def save_live_risk_state(path: Path | str, state: LiveRiskState) -> None:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".live_state_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state.to_dict(), fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, p)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def reset_live_risk_state(
    policy: Policy,
    *,
    equity_usdt: float = 0.0,
    state_path: Path | str | None = None,
) -> LiveRiskState:
    """Clear latched drawdown halt, loss streak cooldown, and reset peak equity."""
    p = Path(state_path).expanduser() if state_path else default_live_state_path(policy)
    state = load_live_risk_state(p)
    state.peak_equity_usdt = round(equity_usdt, 2) if equity_usdt > 0 else 0.0
    state.drawdown_pct = 0.0
    state.drawdown_halted = False
    state.daily_loss_pct = 0.0
    state.daily_loss_halted = False
    state.consecutive_losses = 0
    state.last_loss_ts_ms = 0
    state.cooldown_until_ms = 0
    state.streak_cooldown_active = False
    state.equity_samples = []
    save_live_risk_state(p, state)
    return state


def evaluate_and_update_live_risk_state(
    *,
    policy: Policy,
    equity_usdt: float,
    live_positions: dict[str, dict[str, Any]],
    latest_bar_ts: int = 0,
    symbol_stops: dict[str, tuple[float, float, float]] | None = None,
    rebalance_every: int = 5,
    now_ms: int | None = None,
    state_path: Path | str | None = None,
    persist: bool = True,
) -> LiveRiskState:
    """Update peak equity, 24h PnL, loss streak, and 5-bar rebalance clock."""
    now = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    p = Path(state_path).expanduser() if state_path else default_live_state_path(policy)
    state = load_live_risk_state(p)
    stops_map = symbol_stops or {}

    # 1. Peak equity & peak drawdown stop (latched until manual reset)
    if equity_usdt > state.peak_equity_usdt:
        state.peak_equity_usdt = round(equity_usdt, 2)
    if state.peak_equity_usdt > 0:
        state.drawdown_pct = round(
            max(0.0, (state.peak_equity_usdt - equity_usdt) / state.peak_equity_usdt * 100.0),
            2,
        )
    else:
        state.drawdown_pct = 0.0
    if drawdown_halt(state.peak_equity_usdt, equity_usdt, policy):
        state.drawdown_halted = True

    # 2. Rolling 24h loss halt
    cutoff_7d = now - 7 * 86_400_000
    cutoff_24h = now - 86_400_000
    samples = [
        s
        for s in state.equity_samples
        if isinstance(s, dict) and int(s.get("ts_ms", 0)) >= cutoff_7d
    ]
    older_or_at_24h = [s for s in samples if int(s["ts_ms"]) <= cutoff_24h]
    in_last_24h = [s for s in samples if int(s["ts_ms"]) > cutoff_24h]
    if older_or_at_24h:
        ref_24h = float(older_or_at_24h[-1]["equity"])
    elif in_last_24h:
        ref_24h = max(float(s["equity"]) for s in in_last_24h)
    else:
        ref_24h = equity_usdt
    state.equity_24h_ago_usdt = round(ref_24h, 2)
    state.pnl_24h_usdt = round(equity_usdt - ref_24h, 2)
    state.daily_loss_pct = (
        round(max(0.0, -state.pnl_24h_usdt / ref_24h * 100.0), 2) if ref_24h > 0 else 0.0
    )
    state.daily_loss_halted = bool(daily_loss_halt(state.pnl_24h_usdt, ref_24h, policy))

    if (
        samples
        and (now - int(samples[-1]["ts_ms"]) < 60_000)
        and abs(float(samples[-1]["equity"]) - round(equity_usdt, 2)) < 1e-6
    ):
        samples[-1]["ts_ms"] = now
    else:
        samples.append({"ts_ms": now, "equity": round(equity_usdt, 2)})
    state.equity_samples = samples[-256:]

    # 3. Position outcomes & consecutive loss streak cooldown
    if state.cooldown_until_ms > 0 and now >= state.cooldown_until_ms:
        state.consecutive_losses = 0
        state.cooldown_until_ms = 0
        state.streak_cooldown_active = False

    was_empty = len(state.open_positions) == 0
    for inst_id, prev_info in list(state.open_positions.items()):
        if inst_id in live_positions:
            continue
        entry_px = float(prev_info.get("entry_price") or 0.0)
        exit_px = (
            float(stops_map[inst_id][0])
            if inst_id in stops_map
            else float(prev_info.get("stop_price") or 0.0)
        )
        eq_entry = float(prev_info.get("equity_at_entry") or equity_usdt)
        is_loss = (
            (exit_px < entry_px)
            if (exit_px > 0 and entry_px > 0)
            else (equity_usdt < eq_entry - 0.05)
        )
        if is_loss:
            state.consecutive_losses += 1
            state.last_loss_ts_ms = now
        else:
            state.consecutive_losses = 0
            state.cooldown_until_ms = 0
        del state.open_positions[inst_id]

    for inst_id, pos in live_positions.items():
        qty = abs(float(pos.get("qty") or 0.0))
        notional = float(pos.get("notional_usdt") or 0.0)
        cur_px = (
            notional / qty
            if qty > 0 and notional > 0
            else (float(stops_map[inst_id][0]) if inst_id in stops_map else 0.0)
        )
        if inst_id not in state.open_positions:
            state.open_positions[inst_id] = {
                "qty": qty,
                "entry_price": round(cur_px, 6),
                "last_price": round(cur_px, 6),
                "stop_price": float(pos.get("stop_price") or 0.0),
                "equity_at_entry": round(equity_usdt, 2),
            }
        else:
            state.open_positions[inst_id]["qty"] = qty
            if cur_px > 0:
                state.open_positions[inst_id]["last_price"] = round(cur_px, 6)
            state.open_positions[inst_id]["stop_price"] = float(pos.get("stop_price") or 0.0)

    if streak_cooldown(state.consecutive_losses, policy):
        if state.cooldown_until_ms <= 0:
            anchor_ts = state.last_loss_ts_ms if state.last_loss_ts_ms > 0 else now
            state.cooldown_until_ms = anchor_ts + int(policy.limits.cooldown_hours * 3_600_000)
        state.streak_cooldown_active = now < state.cooldown_until_ms
        if not state.streak_cooldown_active:
            state.consecutive_losses = 0
            state.cooldown_until_ms = 0
    else:
        state.streak_cooldown_active = False

    # 4. 5-bar rebalance cadence alignment
    eff_rebalance_every = max(1, int(rebalance_every))
    if not live_positions:
        state.bars_since_rebalance = eff_rebalance_every
        state.rebalance_due = True
    else:
        if was_empty and state.last_rebalance_bar_ts <= 0 and latest_bar_ts > 0:
            state.last_rebalance_bar_ts = latest_bar_ts
        if state.last_rebalance_bar_ts <= 0 or latest_bar_ts <= 0:
            state.bars_since_rebalance = eff_rebalance_every
            state.rebalance_due = True
        else:
            elapsed = max(
                0, int(round((latest_bar_ts - state.last_rebalance_bar_ts) / 86_400_000))
            )
            state.bars_since_rebalance = elapsed
            state.rebalance_due = elapsed >= eff_rebalance_every

    if persist:
        save_live_risk_state(p, state)
    return state


@dataclass(frozen=True, slots=True)
class RunbookStep:
    stage: int
    action: str
    inst_id: str
    wallet: str
    amount_usdt: float
    qty: float
    stop_price: float
    detail: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExecutionRunbook:
    plan: ShadowPlan
    spot_free_usdt: float
    future_free_usdt: float
    earn_flexible_usdt: float
    earn_product_id: str
    live_positions: dict[str, dict[str, Any]]
    steps: tuple[RunbookStep, ...]
    blocked_reason: str
    audit_hash: str
    risk_state: LiveRiskState | None = None
    entry_halt_reason: str = ""


def _hold_order_for_live_position(
    inst_id: str,
    pos: dict[str, Any],
    plan: ShadowPlan,
    order_by_id: dict[str, ShadowOrder],
) -> ShadowOrder:
    """Build a hold-in-place ShadowOrder for an existing position between 5-bar rebalances."""
    cur_qty = abs(float(pos.get("qty") or 0.0))
    cur_notional = float(pos.get("notional_usdt") or 0.0)
    wallet = str(pos.get("wallet") or ("future" if inst_id.endswith("-SWAP") else "spot"))
    existing_order = order_by_id.get(inst_id)
    if existing_order is not None:
        ref_px = existing_order.ref_price
        sd_pct = existing_order.stop_distance_pct
        sp_val = existing_order.stop_price
        score = existing_order.score
        min_not = existing_order.min_notional_usdt
    elif inst_id in plan.symbol_stops:
        ref_px, sd_pct, sp_val = plan.symbol_stops[inst_id]
        score = 0.0
        min_not = 5.0
    else:
        ref_px = (cur_notional / cur_qty) if cur_qty > 0 else 0.0
        sd_pct = 2.0
        sp_val = float(pos.get("stop_price") or round(ref_px * 0.98, 6))
        score = 0.0
        min_not = 5.0
    stop_payload = build_exchange_stop_payload(
        inst_id, wallet=wallet, qty=cur_qty, stop_price=sp_val
    )
    return ShadowOrder(
        inst_id=inst_id,
        wallet=wallet,
        score=score,
        ref_price=ref_px,
        stop_distance_pct=sd_pct,
        stop_price=sp_val,
        target_notional_usdt=round(cur_qty * ref_px, 2) if ref_px > 0 else cur_notional,
        min_notional_usdt=min_not,
        qty=cur_qty,
        executable=True,
        stop_order=stop_payload,
    )


def build_execution_runbook(
    *,
    policy: Policy,
    gateway: Any,
    plan: ShadowPlan,
    symbols: tuple[str, ...] | list[str] = (),
    rebalance_every: int = 5,
    rebalance_drift_threshold: float = 0.10,
    now_ms: int | None = None,
    state_path: Path | str | None = None,
    persist_audit: bool = True,
) -> ExecutionRunbook:
    """Reconcile live wallets, Simple Earn, positions, and stops against ``plan``.

    Produces the exact 7-stage sequence of atomic exchange operations needed to
    transition the account to the target portfolio and keep all idle cash in
    Simple Earn Flexible (`USDT001`):
    1. Cancel stale stops & close exiting positions (`CANCEL_STOP`, `CLOSE_POSITION`).
    2. Sweep freed USDⓈ-M Futures margin back to Spot (`TRANSFER`).
    3. Redeem required USDT shortfall from Simple Earn Flexible (`REDEEM_EARN`).
    4. Transfer margin collateral from Spot to USDⓈ-M Futures (`TRANSFER`).
    5. Execute delta entry/adjustment orders (`ENTRY_ORDER`).
    6. Place or ratchet exchange-native `STOP_MARKET` / `STOP_LOSS_LIMIT` stops (`PLACE_STOP`).
    7. Subscribe leftover Spot USDT back into Simple Earn Flexible (`SUBSCRIBE_EARN`).
    """
    breakdown = (
        gateway.wallet_usdt_breakdown()
        if hasattr(gateway, "wallet_usdt_breakdown")
        else {
            "spot_free": plan.cash_usdt,
            "future_free": 0.0,
            "earn_flexible": plan.earn_amount_usdt,
            "product_id": "USDT001",
        }
    )
    spot_free = float(breakdown.get("spot_free") or 0.0)
    future_free = float(breakdown.get("future_free") or 0.0)
    earn_flex = float(breakdown.get("earn_flexible") or 0.0)
    product_id = str(breakdown.get("product_id") or "USDT001")

    live_pos = (
        gateway.live_positions_and_stops(symbols)
        if hasattr(gateway, "live_positions_and_stops")
        else {}
    )

    eff_state_path = (
        Path(state_path).expanduser() if state_path else default_live_state_path(policy)
    )
    risk_state = evaluate_and_update_live_risk_state(
        policy=policy,
        equity_usdt=plan.equity_usdt,
        live_positions=live_pos,
        latest_bar_ts=plan.latest_bar_ts,
        symbol_stops=plan.symbol_stops,
        rebalance_every=rebalance_every,
        now_ms=now_ms,
        state_path=eff_state_path,
        persist=persist_audit,
    )

    blocked = ""
    entry_halt_reason = ""
    if plan.halted:
        blocked = "kill_switch_engaged"
    elif plan.below_floor:
        blocked = f"below_equity_floor_${policy.equity_floor_usdt:.2f}"
    elif risk_state.drawdown_halted:
        entry_halt_reason = (
            f"peak_drawdown_halt_{risk_state.drawdown_pct:.1f}%_"
            f">={policy.limits.peak_drawdown_stop_pct:.1f}%"
        )
    elif risk_state.daily_loss_halted:
        entry_halt_reason = (
            f"daily_loss_halt_{risk_state.daily_loss_pct:.1f}%_"
            f">={policy.limits.daily_loss_halt_pct:.1f}%"
        )
    elif risk_state.streak_cooldown_active:
        entry_halt_reason = f"streak_cooldown_{risk_state.consecutive_losses}_losses"

    steps: list[RunbookStep] = []
    if not blocked:
        order_by_id = {o.inst_id: o for o in plan.orders if o.executable}
        if risk_state.drawdown_halted:
            # Peak drawdown stop (>= 18%) flattens all open positions to cash/Earn
            target_map: dict[str, ShadowOrder] = {}
        elif risk_state.daily_loss_halted or risk_state.streak_cooldown_active:
            # Daily loss halt or streak cooldown blocks new entries/increases while
            # keeping uptrend positions with stop ratcheting and exiting broken trends
            target_map = {
                inst_id: _hold_order_for_live_position(inst_id, pos, plan, order_by_id)
                for inst_id, pos in sorted(live_pos.items())
                if inst_id in plan.uptrend_symbols
            }
        elif not risk_state.rebalance_due and live_pos:
            # Mid-cycle (bars 1..4 of 5-bar rebalance window): hold uptrending positions
            # without rotational turnover or drift churn, while ratcheting stops upward
            kept_map = {
                inst_id: _hold_order_for_live_position(inst_id, pos, plan, order_by_id)
                for inst_id, pos in sorted(live_pos.items())
                if inst_id in plan.uptrend_symbols
            }
            if kept_map:
                target_map = kept_map
            else:
                # All held positions broke their trend -> back in cash, allow new top-2
                risk_state.rebalance_due = True
                target_map = order_by_id
        else:
            target_map = order_by_id

        # Stage 1: Cancel stops & close positions not in target_map
        sim_spot = spot_free
        sim_future = future_free
        for inst_id, pos in sorted(live_pos.items()):
            if inst_id in target_map:
                continue
            qty = abs(float(pos["qty"]))
            notional = float(pos["notional_usdt"])
            wallet = str(pos["wallet"])
            if pos.get("stop_order_id"):
                steps.append(
                    RunbookStep(
                        stage=1,
                        action="CANCEL_STOP",
                        inst_id=inst_id,
                        wallet=wallet,
                        amount_usdt=0.0,
                        qty=qty,
                        stop_price=float(pos.get("stop_price") or 0.0),
                        detail=f"cancel open stop {pos['stop_order_id']} before exit",
                        params={"order_id": pos["stop_order_id"]},
                    )
                )
            steps.append(
                RunbookStep(
                    stage=1,
                    action="CLOSE_POSITION",
                    inst_id=inst_id,
                    wallet=wallet,
                    amount_usdt=notional,
                    qty=qty,
                    stop_price=0.0,
                    detail=f"market sell {qty:g} {inst_id} (~${notional:.2f})",
                    params={"reduceOnly": wallet == "future"},
                )
            )
            if wallet == "spot":
                sim_spot += notional * 0.998
            else:
                sim_future += notional * 0.45

        # Compute required cash in Spot and margin in Futures for target orders
        req_spot_delta = 0.0
        req_future_margin = 0.0
        entry_candidates: list[tuple[ShadowOrder, float, float, str]] = []
        for inst_id, order in target_map.items():
            cur = live_pos.get(inst_id)
            cur_qty = float(cur["qty"]) if cur is not None else 0.0
            delta_qty = order.qty - cur_qty
            if hasattr(gateway, "round_order") and abs(delta_qty) > 0:
                r_qty, _ = gateway.round_order(inst_id, abs(delta_qty), order.ref_price)
                delta_qty = r_qty if delta_qty > 0 else -r_qty
            delta_notional = abs(delta_qty) * order.ref_price
            drift_frac = (
                delta_notional / order.target_notional_usdt
                if order.target_notional_usdt > 0
                else 0.0
            )
            needs_trade = (
                cur is None
                or (
                    delta_notional >= order.min_notional_usdt
                    and drift_frac >= rebalance_drift_threshold
                )
            )
            if needs_trade and abs(delta_qty) > 0:
                side = "buy" if delta_qty > 0 else "sell"
                entry_candidates.append((order, abs(delta_qty), round(delta_notional, 2), side))
                if order.wallet == "spot" and side == "buy":
                    req_spot_delta += delta_notional * 1.005
                elif order.wallet == "spot" and side == "sell":
                    sim_spot += delta_notional * 0.995
            if order.wallet == "future":
                # Back each perpetual leg with ~50% margin collateral (2.0x effective leverage,
                # keeping liquidation >45% away while leaving excess cash in Simple Earn)
                req_future_margin += round(order.target_notional_usdt * 0.50, 2)

        # If a scheduled 5-bar rebalance found existing positions already aligned (no trade needed),
        # advance the rebalance bar clock now so the next 5-bar window begins cleanly.
        has_trade_step = any(
            s.action == "CLOSE_POSITION" for s in steps
        ) or bool(entry_candidates)
        if (
            risk_state.rebalance_due
            and live_pos
            and target_map
            and not has_trade_step
            and plan.latest_bar_ts > 0
        ):
            risk_state.last_rebalance_bar_ts = plan.latest_bar_ts
            risk_state.bars_since_rebalance = 0
            if persist_audit:
                save_live_risk_state(eff_state_path, risk_state)

        # Cap required future margin so Spot + Futures never exceed total available cash
        total_cash_avail = sim_spot + sim_future + earn_flex
        max_fut_avail = max(0.0, total_cash_avail - req_spot_delta - 0.50)
        req_future_margin = min(req_future_margin, round(max_fut_avail, 2))

        # Stage 2: Sweep excess Futures free USDT back to Spot
        if sim_future > req_future_margin + 1.0:
            sweep = round(sim_future - req_future_margin, 2)
            steps.append(
                RunbookStep(
                    stage=2,
                    action="TRANSFER",
                    inst_id="USDT",
                    wallet="future->spot",
                    amount_usdt=sweep,
                    qty=sweep,
                    stop_price=0.0,
                    detail=f"transfer ${sweep:.2f} USDT from USDⓈ-M Futures to Spot",
                    params={"from_wallet": "future", "to_wallet": "spot"},
                )
            )
            sim_future -= sweep
            sim_spot += sweep

        # Stage 3: Redeem needed USDT from Simple Earn Flexible (USDT001 -> SPOT)
        fut_shortfall = max(0.0, req_future_margin - sim_future)
        total_spot_needed = req_spot_delta + fut_shortfall
        if total_spot_needed > sim_spot + 0.01 and earn_flex > 0:
            redeem_amt = min(earn_flex, round(total_spot_needed - sim_spot + 0.25, 2))
            if redeem_amt >= 0.10:
                steps.append(
                    RunbookStep(
                        stage=3,
                        action="REDEEM_EARN",
                        inst_id="USDT",
                        wallet="earn->spot",
                        amount_usdt=redeem_amt,
                        qty=redeem_amt,
                        stop_price=0.0,
                        detail=(
                            f"redeem ${redeem_amt:.2f} USDT from Simple Earn "
                            f"({product_id}) to Spot"
                        ),
                        params={"product_id": product_id, "dest_account": "SPOT"},
                    )
                )
                earn_flex -= redeem_amt
                sim_spot += redeem_amt

        # Stage 4: Transfer required margin from Spot to USDⓈ-M Futures
        if fut_shortfall >= 0.50 and sim_spot >= fut_shortfall:
            xfer_amt = round(fut_shortfall, 2)
            steps.append(
                RunbookStep(
                    stage=4,
                    action="TRANSFER",
                    inst_id="USDT",
                    wallet="spot->future",
                    amount_usdt=xfer_amt,
                    qty=xfer_amt,
                    stop_price=0.0,
                    detail=f"transfer ${xfer_amt:.2f} USDT margin from Spot to USDⓈ-M Futures",
                    params={"from_wallet": "spot", "to_wallet": "future"},
                )
            )
            sim_spot -= xfer_amt
            sim_future += xfer_amt

        # Stage 5: Execute entry / delta orders
        for order, d_qty, d_notional, side in entry_candidates:
            reduce_only = side == "sell" and order.wallet == "future"
            steps.append(
                RunbookStep(
                    stage=5,
                    action="ENTRY_ORDER",
                    inst_id=order.inst_id,
                    wallet=order.wallet,
                    amount_usdt=d_notional,
                    qty=d_qty,
                    stop_price=order.stop_price,
                    detail=(
                        f"market {side} {d_qty:g} {order.inst_id} "
                        f"(~${d_notional:.2f} @ {order.ref_price:,.2f})"
                    ),
                    params={"side": side, "reduceOnly": reduce_only},
                )
            )
            if order.wallet == "spot" and side == "buy":
                sim_spot = max(0.0, sim_spot - d_notional)

        # Stage 6: Place or ratchet exchange-native protective stops on all target positions
        for inst_id, order in target_map.items():
            cur = live_pos.get(inst_id)
            cur_stop = float(cur.get("stop_price") or 0.0) if cur is not None else 0.0
            cur_qty = abs(float(cur.get("qty") or 0.0)) if cur is not None else 0.0
            # Ratchet only upward if already protected at same qty
            same_qty = abs(cur_qty - order.qty) <= 1e-8
            if cur is not None and cur_stop >= order.stop_price and same_qty:
                continue
            if cur is not None and cur.get("stop_order_id"):
                steps.append(
                    RunbookStep(
                        stage=6,
                        action="CANCEL_STOP",
                        inst_id=inst_id,
                        wallet=order.wallet,
                        amount_usdt=0.0,
                        qty=order.qty,
                        stop_price=cur_stop,
                        detail=(
                            f"cancel prior stop {cur['stop_order_id']} "
                            f"(@ {cur_stop:,.2f}) to ratchet higher"
                        ),
                        params={"order_id": cur["stop_order_id"]},
                    )
                )
            stop_kind = order.stop_order.get("type", "STOP_MARKET")
            steps.append(
                RunbookStep(
                    stage=6,
                    action="PLACE_STOP",
                    inst_id=inst_id,
                    wallet=order.wallet,
                    amount_usdt=order.target_notional_usdt,
                    qty=order.qty,
                    stop_price=order.stop_price,
                    detail=(
                        f"place {stop_kind} sell {order.qty:g} {inst_id} "
                        f"@ {order.stop_price:,.2f} (-{order.stop_distance_pct:.1f}%)"
                    ),
                    params=dict(order.stop_order),
                )
            )

        # Stage 7: Auto-subscribe any idle Spot USDT (>= $1.00) back into Simple Earn Flexible
        if sim_spot >= 1.0:
            sub_amt = round(sim_spot, 2)
            steps.append(
                RunbookStep(
                    stage=7,
                    action="SUBSCRIBE_EARN",
                    inst_id="USDT",
                    wallet="spot->earn",
                    amount_usdt=sub_amt,
                    qty=sub_amt,
                    stop_price=0.0,
                    detail=(
                        f"subscribe ${sub_amt:.2f} idle Spot USDT into "
                        f"Simple Earn ({product_id})"
                    ),
                    params={"product_id": product_id},
                )
            )

    if entry_halt_reason and not steps:
        blocked = entry_halt_reason

    audit_hash = ""
    if persist_audit:
        audit = AuditLog(policy.execution.audit_log)
        audit_hash = audit.append(
            {
                "kind": "execution_runbook",
                "policy_digest": policy.digest(),
                "spot_free_usdt": round(spot_free, 2),
                "future_free_usdt": round(future_free, 2),
                "earn_flexible_usdt": round(earn_flex, 2),
                "blocked_reason": blocked,
                "entry_halt_reason": entry_halt_reason,
                "peak_equity_usdt": risk_state.peak_equity_usdt,
                "drawdown_pct": risk_state.drawdown_pct,
                "daily_loss_pct": risk_state.daily_loss_pct,
                "consecutive_losses": risk_state.consecutive_losses,
                "bars_since_rebalance": risk_state.bars_since_rebalance,
                "rebalance_due": risk_state.rebalance_due,
                "steps": [asdict(s) for s in steps],
            }
        )

    return ExecutionRunbook(
        plan=plan,
        spot_free_usdt=round(spot_free, 2),
        future_free_usdt=round(future_free, 2),
        earn_flexible_usdt=round(earn_flex, 2),
        earn_product_id=product_id,
        live_positions=live_pos,
        steps=tuple(steps),
        blocked_reason=blocked,
        audit_hash=audit_hash,
        risk_state=risk_state,
        entry_halt_reason=entry_halt_reason,
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
        raw_qty = float(order["qty"])
        raw_stop = float(order.get("stop_price") or 0.0)
        if hasattr(gateway, "round_order"):
            qty, stop_price = gateway.round_order(inst_id, raw_qty, raw_stop)
        else:
            qty, stop_price = raw_qty, raw_stop
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
                _, lim_px = (
                    gateway.round_order(inst_id, qty, stop_price * 0.998)
                    if hasattr(gateway, "round_order")
                    else (qty, round(stop_price * 0.998, 6))
                )
                stop_res = cli.create_order(
                    market["symbol"],
                    "STOP_LOSS_LIMIT",
                    "sell",
                    qty,
                    lim_px,
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

    def execute_runbook(
        self, runbook: ExecutionRunbook, *, gateway: Any
    ) -> dict[str, Any]:
        """Execute every step of ``runbook`` in sequence when armed."""
        if not self._armed:
            raise ExecutionLocked(
                "router is not armed; no order can leave this process"
            )
        if os.path.exists(os.path.expanduser(self.policy.execution.kill_switch_file)):
            raise ExecutionLocked("kill switch is engaged; runbook aborted")
        if runbook.blocked_reason:
            raise ExecutionLocked(f"runbook blocked: {runbook.blocked_reason}")

        results: list[dict[str, Any]] = []
        for step in runbook.steps:
            act = step.action
            if act == "REDEEM_EARN":
                res = gateway.redeem_earn(
                    step.amount_usdt,
                    product_id=str(step.params.get("product_id") or "USDT001"),
                    dest_account=str(step.params.get("dest_account") or "SPOT"),
                )
            elif act == "SUBSCRIBE_EARN":
                res = gateway.subscribe_earn(
                    step.amount_usdt,
                    product_id=str(step.params.get("product_id") or "USDT001"),
                )
            elif act == "TRANSFER":
                res = gateway.transfer_usdt(
                    step.amount_usdt,
                    from_wallet=str(step.params["from_wallet"]),
                    to_wallet=str(step.params["to_wallet"]),
                )
            elif act == "CANCEL_STOP":
                m = gateway._market(step.inst_id)  # noqa: SLF001
                cli = gateway._client_for(m)  # noqa: SLF001
                oid = step.params.get("order_id")
                res = (
                    cli.cancel_order(oid, m["symbol"])
                    if oid
                    else cli.cancel_all_orders(m["symbol"])
                )
            elif act in ("CLOSE_POSITION", "ENTRY_ORDER"):
                m = gateway._market(step.inst_id)  # noqa: SLF001
                cli = gateway._client_for(m)  # noqa: SLF001
                side = str(step.params.get("side") or "sell")
                exec_qty = step.qty
                if (
                    side == "sell"
                    and step.wallet == "spot"
                    and hasattr(gateway, "sellable_spot_qty")
                ):
                    exec_qty = gateway.sellable_spot_qty(step.inst_id, step.qty)
                params = {"reduceOnly": True} if step.params.get("reduceOnly") else {}
                res = cli.create_order(m["symbol"], "market", side, exec_qty, None, params)
            elif act == "PLACE_STOP":
                m = gateway._market(step.inst_id)  # noqa: SLF001
                cli = gateway._client_for(m)  # noqa: SLF001
                if step.wallet == "future":
                    res = cli.create_order(
                        m["symbol"],
                        "STOP_MARKET",
                        "sell",
                        step.qty,
                        None,
                        {
                            "stopPrice": step.stop_price,
                            "reduceOnly": True,
                            "workingType": "CONTRACT_PRICE",
                        },
                    )
                else:
                    stop_qty = (
                        gateway.sellable_spot_qty(step.inst_id, step.qty)
                        if hasattr(gateway, "sellable_spot_qty")
                        else step.qty
                    )
                    _, lim_px = (
                        gateway.round_order(step.inst_id, stop_qty, step.stop_price * 0.998)
                        if hasattr(gateway, "round_order")
                        else (stop_qty, round(step.stop_price * 0.998, 6))
                    )
                    res = cli.create_order(
                        m["symbol"],
                        "STOP_LOSS_LIMIT",
                        "sell",
                        stop_qty,
                        lim_px,
                        {"stopPrice": step.stop_price, "timeInForce": "GTC"},
                    )
            else:  # pragma: no cover - defensive
                raise ValueError(f"unknown runbook action: {act}")
            results.append(
                {
                    "stage": step.stage,
                    "action": act,
                    "inst_id": step.inst_id,
                    "result": res,
                }
            )

        if runbook.plan.latest_bar_ts > 0 and any(
            r["action"] in ("ENTRY_ORDER", "CLOSE_POSITION") for r in results
        ):
            st_path = default_live_state_path(self.policy)
            st = load_live_risk_state(st_path)
            st.last_rebalance_bar_ts = runbook.plan.latest_bar_ts
            st.bars_since_rebalance = 0
            st.rebalance_due = False
            save_live_risk_state(st_path, st)

        audit = AuditLog(self.policy.execution.audit_log)
        digest = audit.append(
            {
                "kind": "live_runbook_executed",
                "policy_digest": self.policy.digest(),
                "steps_executed": len(results),
                "results": results,
            }
        )
        return {"steps_executed": len(results), "results": results, "audit_hash": digest}
