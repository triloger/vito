from __future__ import annotations

import pytest

from vito.policy import Policy
from vito.risk import (
    Reject,
    SizeRequest,
    daily_loss_halt,
    drawdown_halt,
    liquidation_distance,
    max_leverage_for_stop,
    preflight,
    size_position,
    streak_cooldown,
)

# ---------------------------------------------------------------------------
# sizing
# ---------------------------------------------------------------------------


def test_size_is_driven_by_risk_not_leverage(policy: Policy) -> None:
    """4.0% risk on $200 with a 2% stop is a $400 position, i.e. 2.0x."""
    result = size_position(
        SizeRequest(equity=200.0, instrument="BTC-USDT-SWAP", stop_distance=0.02),
        policy,
    )
    assert result.ok
    assert result.notional == pytest.approx(400.0)
    assert result.leverage == pytest.approx(2.0)
    assert result.risk_amount == pytest.approx(8.0)
    assert result.binding == "risk"


def test_wider_stop_means_smaller_position(policy: Policy) -> None:
    tight = size_position(
        SizeRequest(equity=200.0, instrument="BTC-USDT-SWAP", stop_distance=0.02), policy
    )
    wide = size_position(
        SizeRequest(equity=200.0, instrument="BTC-USDT-SWAP", stop_distance=0.04), policy
    )
    assert wide.notional < tight.notional
    assert wide.risk_amount == pytest.approx(tight.risk_amount, rel=1e-3)


def test_conviction_raises_risk_only_for_whitelisted(policy: Policy) -> None:
    btc = size_position(
        SizeRequest(
            equity=200.0, instrument="BTC-USDT-SWAP", stop_distance=0.02, conviction=True
        ),
        policy,
    )
    eth = size_position(
        SizeRequest(
            equity=200.0, instrument="ETH-USDT-SWAP", stop_distance=0.02, conviction=True
        ),
        policy,
    )
    assert btc.risk_amount == pytest.approx(9.0)
    assert eth.risk_amount == pytest.approx(8.0)


def test_leverage_cap_binds_when_risk_is_large(make_policy) -> None:
    tight = make_policy(risk__max_leverage=1.0)
    result = size_position(
        SizeRequest(equity=200.0, instrument="BTC-USDT-SWAP", stop_distance=0.006), tight
    )
    assert result.ok
    assert result.notional == pytest.approx(200.0)
    assert result.binding == "max_leverage"
    assert result.leverage == pytest.approx(1.0)


def test_gross_exposure_cap_leaves_only_the_remainder(make_policy) -> None:
    result = size_position(
        SizeRequest(
            equity=200.0,
            instrument="BTC-USDT-SWAP",
            stop_distance=0.01,
            gross_exposure=560.0,  # cap is 600
        ),
        make_policy(),
    )
    assert result.ok
    assert result.notional == pytest.approx(40.0)
    assert result.binding == "max_gross_exposure"


def test_rejects_when_gross_exposure_is_full(policy: Policy) -> None:
    result = size_position(
        SizeRequest(
            equity=200.0,
            instrument="BTC-USDT-SWAP",
            stop_distance=0.01,
            gross_exposure=600.0,
        ),
        policy,
    )
    assert result.rejected
    assert result.reason is Reject.NO_CAPACITY


def test_rejects_stop_tighter_than_minimum(policy: Policy) -> None:
    result = size_position(
        SizeRequest(equity=200.0, instrument="BTC-USDT-SWAP", stop_distance=0.002), policy
    )
    assert result.rejected
    assert result.reason is Reject.STOP_TOO_TIGHT


def test_rejects_instrument_outside_whitelist(policy: Policy) -> None:
    result = size_position(
        SizeRequest(equity=200.0, instrument="DOGE-USDT-SWAP", stop_distance=0.01), policy
    )
    assert result.rejected
    assert result.reason is Reject.INSTRUMENT_NOT_ALLOWED


def test_rejects_at_position_limit(policy: Policy) -> None:
    result = size_position(
        SizeRequest(
            equity=200.0, instrument="BTC-USDT-SWAP", stop_distance=0.01, open_positions=2
        ),
        policy,
    )
    assert result.rejected
    assert result.reason is Reject.POSITION_LIMIT


def test_rejects_below_exchange_minimum(policy: Policy) -> None:
    result = size_position(
        SizeRequest(
            equity=200.0, instrument="BTC-USDT-SWAP", stop_distance=0.01, min_notional=5000.0
        ),
        policy,
    )
    assert result.rejected
    assert result.reason is Reject.BELOW_MIN_NOTIONAL


def test_rejects_below_equity_floor(policy: Policy) -> None:
    result = size_position(
        SizeRequest(equity=40.0, instrument="BTC-USDT-SWAP", stop_distance=0.01), policy
    )
    assert result.rejected
    assert result.reason is Reject.EQUITY_FLOOR


def test_liquidation_buffer_binds_in_pathological_config(make_policy) -> None:
    """With a 20x buffer, leverage is forced down to keep liquidation far away."""
    harsh = make_policy(
        risk__max_leverage=25.0,
        risk__max_gross_exposure_mult=5.0,
        risk__liquidation_buffer_mult=40.0,
        risk__risk_per_trade_pct=5.0,
        risk__conviction_risk_pct=5.0,
    )
    result = size_position(
        SizeRequest(equity=200.0, instrument="BTC-USDT-SWAP", stop_distance=0.006), harsh
    )
    assert result.ok
    assert result.binding == "liquidation_buffer"
    assert result.liquidation_distance >= 40 * 0.006 - 1e-9


# ---------------------------------------------------------------------------
# liquidation maths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("leverage", "expected"),
    [(1, 0.996), (2, 0.496), (5, 0.196), (10, 0.096)],
)
def test_liquidation_distance(leverage: float, expected: float) -> None:
    assert liquidation_distance(leverage, 0.004) == pytest.approx(expected, abs=1e-6)


def test_max_leverage_for_stop_is_monotonic() -> None:
    tight = max_leverage_for_stop(0.006, 5.0, 0.004)
    wide = max_leverage_for_stop(0.02, 5.0, 0.004)
    assert tight > wide


# ---------------------------------------------------------------------------
# circuit breakers
# ---------------------------------------------------------------------------


def test_daily_halt_trips_at_limit(policy: Policy) -> None:
    assert not daily_loss_halt(-5.0, 200.0, policy)  # -2.5%
    assert daily_loss_halt(-9.0, 200.0, policy)  # -4.5%


def test_drawdown_halt_trips_at_limit(policy: Policy) -> None:
    assert not drawdown_halt(200.0, 169.0, policy)  # -15.5% (within 18% halt)
    assert drawdown_halt(200.0, 164.0, policy)  # -18.0%


def test_loss_streak_cooldown(policy: Policy) -> None:
    assert not streak_cooldown(3, policy)
    assert streak_cooldown(4, policy)


def test_preflight_kill_switch_wins(tmp_path, make_policy) -> None:
    halt = tmp_path / "HALT"
    halt.write_text("stop")
    p = make_policy(execution__kill_switch_file=str(halt))
    reason = preflight(
        equity=200.0,
        pnl_today=0.0,
        equity_start_of_day=200.0,
        peak_equity=200.0,
        consecutive_losses=0,
        policy=p,
    )
    assert reason is Reject.KILL_SWITCH


def test_preflight_clean_account_passes(policy: Policy) -> None:
    reason = preflight(
        equity=200.0,
        pnl_today=1.0,
        equity_start_of_day=199.0,
        peak_equity=200.0,
        consecutive_losses=0,
        policy=policy,
    )
    assert reason is None
