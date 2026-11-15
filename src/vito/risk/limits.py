"""Position sizing and account-level circuit breakers.

This module is the only place allowed to answer "how big?". It is pure: no
network, no I/O, no side effects. That makes it fully unit-testable, which
matters because a bug here is a bug that moves real money.

Sizing logic
------------
Size is driven by *risk*, not by leverage::

    notional = (equity * risk_pct) / stop_distance

Leverage is then only a ceiling applied on top. At a 1.5% risk budget and a 1%
stop, notional is 1.5x equity — the leverage cap never binds. That is the
point: the stop and the risk budget govern exposure, and the leverage cap only
stops pathological configurations.

Two clamps also apply:

* ``max_gross_exposure_mult`` — gap risk. At 3x, a 33% adverse gap is
  unrecoverable. This is the real constraint, not leverage.
* ``liquidation_buffer_mult`` — liquidation must sit at least N stop-widths
  away, so an ordinary stop always fires well before the exchange does.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from vito.policy import Policy


class Reject(StrEnum):
    """Why a proposed position was refused."""

    INVALID_EQUITY = "invalid_equity"
    INSTRUMENT_NOT_ALLOWED = "instrument_not_allowed"
    STOP_TOO_TIGHT = "stop_too_tight"
    POSITION_LIMIT = "position_limit"
    NO_CAPACITY = "no_capacity"
    LIQUIDATION_TOO_CLOSE = "liquidation_too_close"
    BELOW_MIN_NOTIONAL = "below_min_notional"
    KILL_SWITCH = "kill_switch"
    DAILY_HALT = "daily_halt"
    DRAWDOWN_HALT = "drawdown_halt"
    COOLDOWN = "cooldown"
    EQUITY_FLOOR = "equity_floor"


@dataclass(frozen=True, slots=True)
class SizeRequest:
    """A proposed position, before the policy has its say."""

    equity: float
    instrument: str
    stop_distance: float  # as a fraction: 0.008 == 0.8%
    conviction: bool = False
    open_positions: int = 0
    gross_exposure: float = 0.0  # current total notional, in USDT
    min_notional: float = 0.0  # exchange minimum order notional


@dataclass(frozen=True, slots=True)
class SizeResult:
    ok: bool
    notional: float = 0.0
    risk_amount: float = 0.0
    leverage: float = 0.0
    liquidation_distance: float = 0.0
    binding: str = ""
    reason: Reject | None = None
    detail: str = ""

    @property
    def rejected(self) -> bool:
        return not self.ok


def liquidation_distance(leverage: float, maintenance_margin_rate: float) -> float:
    """Approximate distance to liquidation, as a fraction of entry price.

    For isolated margin, a long is liquidated when the loss consumes the posted
    margin minus the maintenance requirement::

        distance ≈ 1 / leverage - mmr

    The exchange's exact formula also includes fees and tiered maintenance
    margins, so treat this as a conservative estimate.
    """
    if leverage <= 0:
        return 1.0
    return max(0.0, 1.0 / leverage - maintenance_margin_rate)


def max_leverage_for_stop(
    stop_distance: float, buffer_mult: float, maintenance_margin_rate: float
) -> float:
    """Largest leverage that still keeps liquidation ``buffer_mult`` stops away.

    Solving ``1/L - mmr >= k * stop`` for L::

        L <= 1 / (k * stop + mmr)
    """
    denominator = buffer_mult * stop_distance + maintenance_margin_rate
    if denominator <= 0:
        return float("inf")
    return 1.0 / denominator


def size_position(req: SizeRequest, policy: Policy) -> SizeResult:
    """Apply the policy to a proposed position. Never raises on rejection."""
    r = policy.risk

    if req.equity <= 0:
        return SizeResult(ok=False, reason=Reject.INVALID_EQUITY, detail="equity <= 0")

    if req.equity < policy.equity_floor_usdt:
        return SizeResult(
            ok=False,
            reason=Reject.EQUITY_FLOOR,
            detail=f"equity {req.equity:.2f} below floor {policy.equity_floor_usdt:.2f}",
        )

    if not policy.instruments.is_allowed(req.instrument):
        return SizeResult(
            ok=False,
            reason=Reject.INSTRUMENT_NOT_ALLOWED,
            detail=f"{req.instrument} is not on the whitelist",
        )

    if req.stop_distance < policy.min_stop_fraction:
        return SizeResult(
            ok=False,
            reason=Reject.STOP_TOO_TIGHT,
            detail=(
                f"stop {req.stop_distance:.3%} is tighter than the "
                f"{policy.min_stop_fraction:.2%} minimum"
            ),
        )

    if req.open_positions >= r.max_open_positions:
        return SizeResult(
            ok=False,
            reason=Reject.POSITION_LIMIT,
            detail=f"already holding {req.open_positions} of {r.max_open_positions}",
        )

    risk_pct = policy.risk_fraction(conviction=req.conviction, instrument=req.instrument)
    risk_budget = req.equity * risk_pct
    notional = risk_budget / req.stop_distance
    binding = "risk"

    # Clamp 1: per-position leverage ceiling.
    leverage_cap_notional = req.equity * r.max_leverage
    if notional > leverage_cap_notional:
        notional = leverage_cap_notional
        binding = "max_leverage"

    # Clamp 2: liquidation must stay k stop-widths away.
    liq_cap = max_leverage_for_stop(
        req.stop_distance, r.liquidation_buffer_mult, r.maintenance_margin_rate
    )
    if liq_cap < r.max_leverage:
        liq_notional = req.equity * liq_cap
        if notional > liq_notional:
            notional = liq_notional
            binding = "liquidation_buffer"

    # Clamp 3: remaining gross exposure. Gap risk, not leverage.
    gross_cap = req.equity * r.max_gross_exposure_mult
    remaining = gross_cap - req.gross_exposure
    if remaining <= 0:
        return SizeResult(
            ok=False,
            reason=Reject.NO_CAPACITY,
            detail=(
                f"gross exposure {req.gross_exposure:.2f} at cap "
                f"{gross_cap:.2f} ({r.max_gross_exposure_mult}x equity)"
            ),
        )
    if notional > remaining:
        notional = remaining
        binding = "max_gross_exposure"

    if notional < req.min_notional:
        return SizeResult(
            ok=False,
            reason=Reject.BELOW_MIN_NOTIONAL,
            detail=(
                f"computed notional {notional:.2f} below exchange minimum "
                f"{req.min_notional:.2f}"
            ),
        )

    leverage = notional / req.equity
    liq = liquidation_distance(leverage, r.maintenance_margin_rate)
    required = r.liquidation_buffer_mult * req.stop_distance

    if liq < required - 1e-9:
        return SizeResult(
            ok=False,
            reason=Reject.LIQUIDATION_TOO_CLOSE,
            detail=(
                f"liquidation {liq:.2%} away, needs {required:.2%} "
                f"({r.liquidation_buffer_mult}x stop)"
            ),
        )

    return SizeResult(
        ok=True,
        notional=round(notional, 2),
        risk_amount=round(notional * req.stop_distance, 2),
        leverage=round(leverage, 3),
        liquidation_distance=round(liq, 4),
        binding=binding,
    )


# ---------------------------------------------------------------------------
# Account-level circuit breakers
# ---------------------------------------------------------------------------


def daily_loss_halt(pnl_today: float, equity_start_of_day: float, policy: Policy) -> bool:
    """True when the rolling 24h loss limit has been breached."""
    if equity_start_of_day <= 0:
        return True
    loss_pct = -pnl_today / equity_start_of_day * 100.0
    return loss_pct >= policy.limits.daily_loss_halt_pct


def drawdown_halt(peak_equity: float, equity: float, policy: Policy) -> bool:
    """True when equity has fallen far enough from peak to stop everything."""
    if peak_equity <= 0:
        return True
    drawdown_pct = (peak_equity - equity) / peak_equity * 100.0
    return drawdown_pct >= policy.limits.peak_drawdown_stop_pct


def streak_cooldown(consecutive_losses: int, policy: Policy) -> bool:
    """True when a losing streak should force a pause."""
    return consecutive_losses >= policy.limits.loss_streak_cooldown


def kill_switch_engaged(path: str | None) -> bool:
    """True when the operator's halt file exists."""
    if not path:
        return False
    from pathlib import Path

    return Path(path).expanduser().exists()


def preflight(
    *,
    equity: float,
    pnl_today: float,
    equity_start_of_day: float,
    peak_equity: float,
    consecutive_losses: int,
    policy: Policy,
) -> Reject | None:
    """Run every account-level check. Returns the first breach, or ``None``."""
    if kill_switch_engaged(policy.execution.kill_switch_file):
        return Reject.KILL_SWITCH
    if equity < policy.equity_floor_usdt:
        return Reject.EQUITY_FLOOR
    if daily_loss_halt(pnl_today, equity_start_of_day, policy):
        return Reject.DAILY_HALT
    if drawdown_halt(peak_equity, equity, policy):
        return Reject.DRAWDOWN_HALT
    if streak_cooldown(consecutive_losses, policy):
        return Reject.COOLDOWN
    return None
