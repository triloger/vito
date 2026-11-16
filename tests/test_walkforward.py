from __future__ import annotations

import pandas as pd
import pytest

from vito.backtest import walk_forward
from vito.backtest.walkforward import (
    DEFAULT_GRIDS,
    fold_table,
    iter_params,
    oos_buy_and_hold,
)

STEP = 86_400_000


def candles(n: int = 500, start: float = 100.0, drift: float = 0.4) -> pd.DataFrame:
    close = [start + i * drift for i in range(n)]
    return pd.DataFrame(
        {
            "timestamp": [1_700_000_000_000 + i * STEP for i in range(n)],
            "open": close,
            "high": [c + 1 for c in close],
            "low": [c - 1 for c in close],
            "close": close,
            "volume": [10.0] * n,
        }
    )


class AlwaysLong:
    """Deterministic stand-in: alternates long/flat, 2% stop, no conviction."""

    name = "always"

    def signals(self, inst_id: str, frame: pd.DataFrame) -> pd.DataFrame:
        side = [
            "flat" if i < 5 or (i // 10) % 2 else "long" for i in range(len(frame))
        ]
        return pd.DataFrame(
            {
                "side": side,
                "stop_distance": [0.02] * len(frame),
                "conviction": [False] * len(frame),
            }
        )


@pytest.fixture
def long_only(monkeypatch):
    import vito.backtest.walkforward as wf

    monkeypatch.setattr(wf, "build", lambda name, **kw: AlwaysLong())
    return wf


# ---------------------------------------------------------------------------
# grid
# ---------------------------------------------------------------------------


def test_iter_params_is_cartesian() -> None:
    params = iter_params({"a": [1, 2], "b": [3, 4, 5]})
    assert len(params) == 6
    assert {"a": 1, "b": 5} in params


def test_iter_params_drops_fast_not_below_slow() -> None:
    params = iter_params({"fast": [10, 50], "slow": [50], "atr_mult": [2.0]})
    assert params == [
        {"atr_mult": 2.0, "fast": 10, "slow": 50},
    ]


def test_default_grids_cover_the_library() -> None:
    assert set(DEFAULT_GRIDS) == {"trend", "breakout", "meanrev"}
    for grid in DEFAULT_GRIDS.values():
        assert iter_params(grid)


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------


def test_requires_enough_bars() -> None:
    with pytest.raises(ValueError, match="need at least"):
        walk_forward(
            candles(100),
            inst_id="BTC-USDT-SWAP",
            strategy_name="breakout",
            policy=_policy(),
            train_bars=365,
            test_bars=90,
        )


def test_folds_never_test_on_training_bars(long_only) -> None:
    result = walk_forward(
        candles(500),
        inst_id="BTC-USDT-SWAP",
        strategy_name="wat",
        policy=_policy(),
        grid={"window": [10, 20]},
        train_bars=200,
        test_bars=100,
    )
    assert result.n_folds == 3
    for fold in result.folds:
        assert fold.test_start == fold.train_end
        assert fold.test_end > fold.test_start


def test_test_windows_are_disjoint_and_contiguous(long_only) -> None:
    result = walk_forward(
        candles(500),
        inst_id="BTC-USDT-SWAP",
        strategy_name="wat",
        policy=_policy(),
        grid={"window": [10, 20]},
        train_bars=200,
        test_bars=100,
    )
    spans = [(f.test_start, f.test_end) for f in result.folds]
    for (_, prev_end), (next_start, _) in zip(spans, spans[1:], strict=False):
        assert next_start == prev_end


def test_oos_curve_only_covers_traded_folds(long_only) -> None:
    result = walk_forward(
        candles(500),
        inst_id="BTC-USDT-SWAP",
        strategy_name="wat",
        policy=_policy(),
        grid={"window": [10, 20]},
        train_bars=200,
        test_bars=100,
    )
    traded_bars = sum(f.test_end - f.test_start for f in result.folds if f.traded)
    assert len(result.equity_curve) == traded_bars


def test_sitting_out_leaves_equity_untouched(long_only) -> None:
    """With an unreachable bar, no fold may trade and equity must be flat."""
    result = walk_forward(
        candles(500),
        inst_id="BTC-USDT-SWAP",
        strategy_name="wat",
        policy=_policy(),
        grid={"window": [10]},
        train_bars=200,
        test_bars=100,
        min_train_trades=10_000,
    )
    assert result.n_traded == 0
    assert result.n_trades == 0
    assert result.metrics["total_return_pct"] == 0.0


def test_oos_return_chains_across_folds(long_only) -> None:
    result = walk_forward(
        candles(500),
        inst_id="BTC-USDT-SWAP",
        strategy_name="wat",
        policy=_policy(),
        grid={"window": [10]},
        train_bars=200,
        test_bars=100,
    )
    growth = 1.0
    for fold in result.folds:
        growth *= 1 + fold.test_return_pct / 100
    assert result.metrics["total_return_pct"] == pytest.approx((growth - 1) * 100, abs=0.05)


def test_benchmark_uses_the_same_span(long_only) -> None:
    frame = candles(500)
    result = walk_forward(
        frame,
        inst_id="BTC-USDT-SWAP",
        strategy_name="wat",
        policy=_policy(),
        grid={"window": [10]},
        train_bars=200,
        test_bars=100,
    )
    start, end = result.folds[0].test_start, result.folds[-1].test_end
    expected = (frame["close"].iloc[end - 1] / frame["close"].iloc[start] - 1) * 100
    assert oos_buy_and_hold(frame, result)["total_return_pct"] == pytest.approx(expected, abs=0.05)


def test_fold_table_is_renderable(long_only) -> None:
    result = walk_forward(
        candles(500),
        inst_id="BTC-USDT-SWAP",
        strategy_name="wat",
        policy=_policy(),
        grid={"window": [10]},
        train_bars=200,
        test_bars=100,
    )
    rows = fold_table(result)
    assert len(rows) == result.n_folds
    assert all({"fold", "params", "test_return_pct"} <= set(r) for r in rows)


def _policy():
    from vito.policy import load_policy

    return load_policy()
