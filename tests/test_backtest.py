from __future__ import annotations

import pandas as pd
import pytest

from vito.backtest import buy_and_hold, run_backtest
from vito.strategy import STRATEGIES, build

STEP = 3_600_000


def candles(opens, highs, lows, closes, start: int = 1_700_000_000_000) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": [start + i * STEP for i in range(len(opens))],
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1.0] * len(opens),
        }
    )


def signals(sides, stop: float = 0.01) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "side": sides,
            "stop_distance": [stop] * len(sides),
            "conviction": [False] * len(sides),
        }
    )


def run(frame, sig, policy, **kwargs):
    return run_backtest(
        frame,
        sig,
        inst_id="BTC-USDT-SWAP",
        strategy_name="test",
        policy=policy,
        timeframe="1h",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# timing
# ---------------------------------------------------------------------------


def test_entry_is_next_bar_open_not_signal_bar(policy) -> None:
    frame = candles(
        opens=[100, 110, 110, 110],
        highs=[101, 111, 111, 111],
        lows=[99, 109, 109, 109],
        closes=[100, 110, 110, 110],
    )
    sig = signals(["long", "flat", "flat", "flat"])

    result = run(frame, sig, policy, slippage_bps=0)
    assert result.n_trades == 1
    trade = result.trades[0]
    assert trade.entry_ts == int(frame["timestamp"].iloc[1])
    assert trade.entry_price == pytest.approx(110.0)
    assert trade.exit_ts == int(frame["timestamp"].iloc[2])


def test_no_signal_means_no_trades(policy) -> None:
    frame = candles([100] * 5, [101] * 5, [99] * 5, [100] * 5)
    result = run(frame, signals(["flat"] * 5), policy)
    assert result.n_trades == 0
    assert result.final_equity == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# stops
# ---------------------------------------------------------------------------


def test_stop_fills_at_stop_price(policy) -> None:
    frame = candles(
        opens=[100, 100, 100],
        highs=[101, 101, 101],
        lows=[99, 98, 98],
        closes=[100, 99, 99],
    )
    result = run(frame, signals(["long", "flat", "flat"]), policy, slippage_bps=0)
    assert result.trades[0].exit_reason == "stop"
    assert result.trades[0].exit_price == pytest.approx(99.0)


def test_gap_through_stop_fills_at_open(policy) -> None:
    frame = candles(
        opens=[100, 100, 90],
        highs=[101, 105, 91],
        lows=[99, 99.5, 89],
        closes=[100, 104, 90],
    )
    result = run(frame, signals(["long", "flat", "flat"]), policy, slippage_bps=0)
    trade = result.trades[0]
    assert trade.exit_reason == "stop"
    assert trade.exit_price == pytest.approx(90.0)


def test_stop_too_tight_is_refused_by_policy(policy) -> None:
    frame = candles([100] * 4, [101] * 4, [99] * 4, [100] * 4)
    result = run(frame, signals(["long"] * 4, stop=0.001), policy)
    assert result.n_trades == 0
    assert result.skipped.get("stop_too_tight", 0) >= 1


# ---------------------------------------------------------------------------
# costs
# ---------------------------------------------------------------------------


def test_fees_are_charged_on_both_legs(policy) -> None:
    frame = candles([100] * 4, [101] * 4, [99.5] * 4, [100] * 4)
    result = run(frame, signals(["long", "flat", "flat", "flat"]), policy, fee_bps=5.0)
    trade = result.trades[0]
    assert trade.fees == pytest.approx(trade.notional * 0.0005 * 2, rel=1e-6)


def test_slippage_moves_fills_against_you(policy) -> None:
    frame = candles([100] * 4, [101] * 4, [99.5] * 4, [100] * 4)
    result = run(frame, signals(["long", "flat", "flat", "flat"]), policy, slippage_bps=2.0)
    trade = result.trades[0]
    assert trade.entry_price > 100
    assert trade.exit_price < 100


def test_funding_is_charged_to_longs(policy) -> None:
    frame = candles([100] * 8, [101] * 8, [99.5] * 8, [100] * 8)
    funding = pd.Series([0.0002], index=[int(frame["timestamp"].iloc[3])])
    result = run(
        frame,
        signals(["long"] * 8),
        policy,
        funding=funding,
        max_bars=3,
    )
    assert result.trades
    assert result.trades[0].funding > 0


def test_align_funding_assigns_intrabar_and_drops_pre_window() -> None:
    import numpy as np

    from vito.backtest.engine import _align_funding

    day = 86_400_000
    t0 = 1_700_000_000_000
    ts = np.array([t0, t0 + day, t0 + 2 * day], dtype=np.int64)
    funding = pd.Series(
        [0.01, 0.0001, 0.0002, 0.0003, 0.0004, 0.09],
        index=[
            t0 - 1000,  # before window -> dropped
            t0,  # day 0 00:00 -> bar 0
            t0 + 8 * 3_600_000,  # day 0 08:00 -> bar 0
            t0 + 16 * 3_600_000,  # day 0 16:00 -> bar 0
            t0 + day + 8 * 3_600_000,  # day 1 08:00 -> bar 1
            t0 + 3 * day,  # after window -> dropped
        ],
    )
    aligned = _align_funding(funding, ts, day)
    assert aligned[0] == pytest.approx(0.0006)
    assert aligned[1] == pytest.approx(0.0004)
    assert aligned[2] == pytest.approx(0.0)


def test_min_notional_refuses_undersized_orders(policy) -> None:
    frame = candles([100] * 4, [101] * 4, [99.5] * 4, [100] * 4)
    result = run(frame, signals(["long", "flat", "flat", "flat"]), policy, min_notional=5_000.0)
    assert result.n_trades == 0
    assert result.skipped.get("below_min_notional", 0) >= 1


# ---------------------------------------------------------------------------
# accounting
# ---------------------------------------------------------------------------


def test_equity_curve_matches_final_equity(policy) -> None:
    frame = candles(
        opens=[100, 100, 105, 110],
        highs=[101, 106, 111, 111],
        lows=[99, 99, 104, 109],
        closes=[100, 105, 110, 110],
    )
    result = run(frame, signals(["long", "flat", "flat", "flat"]), policy)
    assert result.final_equity == pytest.approx(float(result.equity_curve.iloc[-1]), abs=0.01)


def test_mismatched_lengths_raise(policy) -> None:
    frame = candles([100] * 4, [101] * 4, [99] * 4, [100] * 4)
    with pytest.raises(ValueError):
        run(frame, signals(["flat"] * 3), policy)


def test_buy_and_hold_tracks_price(policy) -> None:
    frame = candles([100] * 5, [100] * 5, [100] * 5, [100, 110, 120, 130, 140])
    metrics = buy_and_hold(frame, initial_equity=200.0, timeframe="1h")
    assert metrics["total_return_pct"] == pytest.approx(40.0, abs=0.01)


# ---------------------------------------------------------------------------
# strategy library
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_strategies_emit_valid_frames(name: str) -> None:
    n = 300
    close = [100 + (i % 20) - 10 for i in range(n)]
    frame = candles(
        opens=close,
        highs=[c + 1 for c in close],
        lows=[c - 1 for c in close],
        closes=close,
    )
    sig = build(name).signals("BTC-USDT-SWAP", frame)

    assert list(sig.columns) == ["side", "stop_distance", "conviction"]
    assert len(sig) == n
    assert set(sig["side"].unique()) <= {"long", "short", "flat"}
    assert (sig["stop_distance"].dropna() > 0).all()


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_unknown_strategy_raises(name: str) -> None:
    with pytest.raises(ValueError):
        build("nope")


def test_strategy_has_no_lookahead(policy) -> None:
    """Truncating the future must not change a past signal."""
    n = 200
    close = [100 + ((i * 7) % 13) for i in range(n)]
    frame = candles(close, [c + 1 for c in close], [c - 1 for c in close], close)

    full = build("trend").signals("BTC-USDT-SWAP", frame)
    truncated = build("trend").signals("BTC-USDT-SWAP", frame.iloc[:120])

    pd.testing.assert_frame_equal(full.iloc[:120], truncated)
