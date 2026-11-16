"""Walk-forward validation.

A single backtest answers "did this configuration work on this data?" — which is
the wrong question, because the configuration was chosen *after* seeing the data.
Walk-forward asks the question that matters: **would choosing parameters from the
past have worked in the future?**

The procedure, per fold:

1. Rank every parameter combination on a *train* window.
2. Pick the best one that traded often enough to be meaningful.
3. Run exactly that combination on the next, untouched *test* window.
4. Roll forward and repeat.

Then the test windows are chained into one out-of-sample equity curve. Nothing in
that curve was fitted to the bars it earned money on.

Two deliberate conservatism choices:

* The strategy starts **flat** at every test boundary, so no position or PnL is
  carried across a fold. This loses a little performance and removes a way to
  accidentally leak the training period into the test period.
* If no combination clears the minimum trade count **and** the minimum train
  score, the fold **does not trade**. Sitting out is a real decision and it is
  recorded as one.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from vito.backtest.engine import (
    BARS_PER_YEAR,
    BacktestResult,
    Trade,
    compute_metrics,
    run_backtest,
)
from vito.policy import Policy
from vito.strategy import build

# ---------------------------------------------------------------------------
# search spaces
# ---------------------------------------------------------------------------

# Deliberately coarse. A fine grid on two years of daily bars is a curve-fitting
# machine: it finds the parameters that best memorise noise.
DEFAULT_GRIDS: dict[str, dict[str, list[Any]]] = {
    "trend": {
        "fast": [10, 20, 30],
        "slow": [50, 80, 100],
        "atr_mult": [1.5, 2.0, 2.5, 3.0],
    },
    "breakout": {
        "window": [10, 15, 20, 25, 30, 40, 55],
        "atr_mult": [1.5, 2.0, 2.5, 3.0],
        "volume_mult": [1.0, 1.5, 2.0],
    },
    "meanrev": {
        "window": [24, 48, 72],
        "entry_z": [1.5, 2.0, 2.5],
        "exit_z": [0.3, 0.5, 0.8],
    },
}


def iter_params(grid: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """Cartesian product of a grid, dropping impossible combinations."""
    keys = sorted(grid)
    out: list[dict[str, Any]] = []
    for values in itertools.product(*(grid[k] for k in keys)):
        params = dict(zip(keys, values, strict=True))
        if "fast" in params and "slow" in params and params["fast"] >= params["slow"]:
            continue
        out.append(params)
    return out


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Fold:
    index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    params: dict[str, Any] | None
    train_score: float
    train_trades: int
    result: BacktestResult | None
    test_return_pct: float

    @property
    def traded(self) -> bool:
        return self.params is not None


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    inst_id: str
    strategy: str
    timeframe: str
    folds: tuple[Fold, ...]
    equity_curve: pd.Series
    trades: tuple[Trade, ...]
    metrics: dict[str, float] = field(default_factory=dict)
    selection: dict[str, float] = field(default_factory=dict)

    @property
    def n_folds(self) -> int:
        return len(self.folds)

    @property
    def n_traded(self) -> int:
        return sum(1 for f in self.folds if f.traded)

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def params_chosen(self) -> list[dict[str, Any]]:
        return [f.params for f in self.folds if f.params is not None]


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------


def walk_forward(
    candles: pd.DataFrame,
    *,
    inst_id: str,
    strategy_name: str,
    policy: Policy,
    grid: dict[str, list[Any]] | None = None,
    train_bars: int = 365,
    test_bars: int = 90,
    initial_equity: float = 200.0,
    fee_bps: float = 5.0,
    slippage_bps: float = 2.0,
    funding: pd.Series | None = None,
    timeframe: str = "1d",
    objective: str = "sharpe",
    min_train_trades: int = 4,
    min_train_score: float = 0.0,
    max_bars: int = 90,
    min_notional: float = 0.0,
) -> WalkForwardResult:
    """Roll a parameter search forward through ``candles`` and score it blind."""
    grid = grid or DEFAULT_GRIDS.get(strategy_name)
    if not grid:
        raise ValueError(f"no parameter grid for strategy {strategy_name!r}")

    combos = iter_params(grid)
    if not combos:
        raise ValueError("parameter grid produced no valid combinations")

    candles = candles.reset_index(drop=True)
    n = len(candles)
    if n < train_bars + test_bars:
        raise ValueError(
            f"need at least train_bars + test_bars = {train_bars + test_bars} bars, have {n}"
        )

    # Signals depend only on data at or before their own bar, so computing them
    # once over the full series and slicing is not lookahead. Indicator warm-up
    # is paid for using bars that precede each test window, which is exactly what
    # a live system would have had.
    signal_cache: dict[tuple[tuple[str, Any], ...], pd.DataFrame] = {}
    for params in combos:
        key = tuple(sorted(params.items()))
        signal_cache[key] = build(strategy_name, **params).signals(inst_id, candles)

    folds: list[Fold] = []
    pieces: list[pd.Series] = []
    all_trades: list[Trade] = []
    scores: list[float] = []

    index = 0
    while True:
        train_start = index
        train_end = train_start + train_bars
        test_start = train_end
        test_end = test_start + test_bars
        if test_end > n:
            break

        train_candles = candles.iloc[train_start:train_end].reset_index(drop=True)

        best: tuple[float, dict[str, Any], int] | None = None
        for params in combos:
            key = tuple(sorted(params.items()))
            signals = signal_cache[key].iloc[train_start:train_end].reset_index(drop=True)
            result = run_backtest(
                train_candles,
                signals,
                inst_id=inst_id,
                strategy_name=strategy_name,
                policy=policy,
                initial_equity=initial_equity,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
                funding=funding,
                timeframe=timeframe,
                max_bars=max_bars,
                min_notional=min_notional,
            )
            if result.n_trades < min_train_trades:
                continue
            score = _score(result.metrics, objective)
            if not math.isfinite(score) or score <= min_train_score:
                continue
            if best is None or score > best[0]:
                best = (score, params, result.n_trades)

        fold_index = len(folds)
        test_candles = candles.iloc[test_start:test_end].reset_index(drop=True)

        if best is None:
            # Nothing traded enough to be worth trusting: sit out.
            # Equity remains flat across the sat-out fold's calendar bars so
            # OOS CAGR and Sharpe are not inflated by shrinking the time span.
            folds.append(
                Fold(
                    index=fold_index,
                    train_start=train_start,
                    train_end=train_end,
                    test_start=test_start,
                    test_end=test_end,
                    params=None,
                    train_score=0.0,
                    train_trades=0,
                    result=None,
                    test_return_pct=0.0,
                )
            )
            scores.append(0.0)
            prev_end = float(pieces[-1].iloc[-1]) if pieces else 1.0
            flat_piece = pd.Series(
                [prev_end] * len(test_candles),
                index=pd.to_datetime(test_candles["timestamp"], unit="ms"),
                dtype=float,
            )
            pieces.append(flat_piece)
            index += test_bars
            continue

        score, params, train_trades = best
        key = tuple(sorted(params.items()))

        test_signals = signal_cache[key].iloc[test_start:test_end].reset_index(drop=True)
        test_result = run_backtest(
            test_candles,
            test_signals,
            inst_id=inst_id,
            strategy_name=strategy_name,
            policy=policy,
            initial_equity=initial_equity,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            funding=funding,
            timeframe=timeframe,
            max_bars=max_bars,
            min_notional=min_notional,
        )

        fold_return = test_result.final_equity / initial_equity - 1.0
        folds.append(
            Fold(
                index=fold_index,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                params=params,
                train_score=round(score, 4),
                train_trades=train_trades,
                result=test_result,
                test_return_pct=round(fold_return * 100, 2),
            )
        )
        scores.append(score)
        all_trades.extend(test_result.trades)

        rebased = test_result.equity_curve / initial_equity
        if pieces:
            rebased = rebased * pieces[-1].iloc[-1]
        pieces.append(rebased)

        index += test_bars

    if not folds:
        raise ValueError("no complete folds fit in the data")

    if pieces:
        # pieces are growth factors (1.0 == untouched), so chaining is a product
        curve = pd.concat(pieces) * initial_equity
        curve.name = "equity"
    else:
        curve = pd.Series([initial_equity], dtype=float, name="equity")

    metrics = compute_metrics(
        curve, all_trades, initial_equity, timeframe, n_trials=len(combos)
    )
    metrics["folds"] = float(len(folds))
    metrics["folds_traded"] = float(sum(1 for f in folds if f.traded))
    metrics["folds_positive"] = float(sum(1 for f in folds if f.test_return_pct > 0))
    metrics["trials_per_fold"] = float(len(combos))
    metrics["fees_paid"] = round(
        sum((f.result.metrics.get("fees_paid", 0.0) if f.result else 0.0) for f in folds), 2
    )
    metrics["funding_paid"] = round(
        sum((f.result.metrics.get("funding_paid", 0.0) if f.result else 0.0) for f in folds),
        2,
    )

    selection = _selection_stability(folds)

    return WalkForwardResult(
        inst_id=inst_id,
        strategy=strategy_name,
        timeframe=timeframe,
        folds=tuple(folds),
        equity_curve=curve,
        trades=tuple(all_trades),
        metrics=metrics,
        selection=selection,
    )


def _score(metrics: dict[str, float], objective: str) -> float:
    if objective == "sharpe":
        return float(metrics.get("sharpe", 0.0))
    if objective == "expectancy":
        return float(metrics.get("expectancy_r", 0.0))
    if objective == "return_dd":
        dd = abs(float(metrics.get("max_drawdown_pct", 0.0)))
        return float(metrics.get("total_return_pct", 0.0)) / dd if dd > 0 else 0.0
    raise ValueError(f"unknown objective {objective!r}")


def _selection_stability(folds: list[Fold]) -> dict[str, float]:
    """How often the search picked the same value for each parameter."""
    traded = [f.params for f in folds if f.params is not None]
    if not traded:
        return {}
    keys = sorted({k for p in traded for k in p})
    out: dict[str, float] = {}
    for k in keys:
        values = [p[k] for p in traded if k in p]
        if not values:
            continue
        top = max(set(values), key=values.count)
        out[k] = round(values.count(top) / len(values), 2)
    return out


def oos_buy_and_hold(
    candles: pd.DataFrame,
    result: WalkForwardResult,
    *,
    initial_equity: float = 200.0,
) -> dict[str, float]:
    """Buy-and-hold over exactly the out-of-sample span, for a fair comparison."""
    start = result.folds[0].test_start
    end = result.folds[-1].test_end
    window = candles.iloc[start:end].reset_index(drop=True)
    if window.empty:
        return {}
    close = window["close"].astype(float)
    index = pd.to_datetime(window["timestamp"], unit="ms")
    curve = pd.Series((initial_equity * close / close.iloc[0]).to_numpy(), index=index)
    per_year = BARS_PER_YEAR.get(result.timeframe, 365)
    years = len(curve) / per_year
    final = float(curve.iloc[-1])
    returns = curve.pct_change().dropna()
    std = float(returns.std())
    running_max = curve.cummax()
    return {
        "total_return_pct": round((final / initial_equity - 1) * 100, 2),
        "cagr_pct": round(((final / initial_equity) ** (1 / years) - 1) * 100, 2)
        if years > 0 and final > 0
        else -100.0,
        "max_drawdown_pct": round(float((curve / running_max - 1).min()) * 100, 2),
        "sharpe": round(float(returns.mean()) / std * math.sqrt(per_year), 2)
        if std > 0
        else 0.0,
    }


def fold_table(result: WalkForwardResult) -> list[dict[str, Any]]:
    """Per-fold rows, ready for a table."""
    rows: list[dict[str, Any]] = []
    for f in result.folds:
        params = (
            " · ".join(f"{k}={v}" for k, v in sorted(f.params.items()))
            if f.params
            else "— sat out —"
        )
        rows.append(
            {
                "fold": f.index + 1,
                "params": params,
                "train_score": f.train_score,
                "train_trades": f.train_trades,
                "test_return_pct": f.test_return_pct,
                "test_trades": f.result.n_trades if f.result else 0,
            }
        )
    return rows
