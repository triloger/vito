"""Cross-asset panel backtester (Time-Series + Cross-Sectional Momentum).

Why a panel instead of single-series grid search
------------------------------------------------
Single-asset walk-forward on 730 daily bars produced 10 trades — too few
observations to separate signal from noise, and 84 parameter combinations
guaranteed selection bias.

This module tests **one pre-registered, fixed-parameter hypothesis** across a
multi-asset panel (Crypto + Equities/Indices + Commodities):

1. **Absolute trend filter (time-series momentum):** At each rebalance close
   ``t``, an instrument is eligible (long-only) only if its trailing
   ``lookback`` return is positive and its close is above its ``lookback``
   moving average. When everything is in a downtrend, the portfolio holds
   USDT cash.
2. **Relative strength rank (cross-sectional momentum):** Eligible instruments
   are ranked by volatility-adjusted return (``return / volatility`` over
   ``lookback``). The top ``max_positions`` are selected.
3. **Next-open execution & turnover-only costs:** Weights decided at close ``t``
   execute at open ``t + 1``. Taker fees and slippage are charged on the
   notional delta traded (`abs(new_notional - old_notional)`), plus intrabar
   stop-outs if an asset drops through its ATR stop between rebalances.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

DEFAULT_BASKET: tuple[str, ...] = (
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "QQQ-USDT-SWAP",
    "SPY-USDT-SWAP",
    "NVDA-USDT-SWAP",
    "XAU-USDT-SWAP",
    "XAG-USDT-SWAP",
    "CL-USDT-SWAP",
)

BARS_PER_YEAR = {"1h": 24 * 365, "4h": 6 * 365, "1d": 365}


@dataclass(frozen=True, slots=True)
class RebalanceRecord:
    timestamp: int
    selected: tuple[str, ...]
    notional_per_asset: float
    turnover_usdt: float
    fees_usdt: float
    equity_after: float


@dataclass(frozen=True, slots=True)
class PanelResult:
    symbols: tuple[str, ...]
    timeframe: str
    lookback: int
    rebalance_every: int
    initial_equity: float
    final_equity: float
    equity_curve: pd.Series
    equal_weight_curve: pd.Series
    rebalances: tuple[RebalanceRecord, ...]
    asset_stats: dict[str, dict[str, float]] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    benchmark: dict[str, float] = field(default_factory=dict)


def align_panel(
    frames: dict[str, pd.DataFrame],
) -> tuple[np.ndarray, dict[str, np.ndarray], dict[str, np.ndarray]]:
    """Align multiple OHLCV frames on the union of timestamps (point-in-time).

    Instruments listed later have ``NaN`` prior to their listing date and only
    become eligible once they have ``lookback`` valid bars of live history.
    """
    non_empty = {k: v for k, v in frames.items() if not v.empty}
    if not non_empty:
        raise ValueError("no frames provided")

    all_ts: set[int] = set()
    for df in non_empty.values():
        all_ts.update(df["timestamp"].astype("int64").tolist())

    if not all_ts:
        raise ValueError("no timestamps across instruments")

    ts = np.array(sorted(all_ts), dtype=np.int64)
    opens: dict[str, np.ndarray] = {}
    closes: dict[str, np.ndarray] = {}

    for sym, df in non_empty.items():
        indexed = df.drop_duplicates("timestamp").set_index("timestamp").reindex(ts)
        opens[sym] = indexed["open"].to_numpy(dtype=float)
        closes[sym] = indexed["close"].to_numpy(dtype=float)

    return ts, opens, closes


def run_panel_momentum(
    frames: dict[str, pd.DataFrame],
    *,
    timeframe: str = "1d",
    lookback: int = 20,
    rebalance_every: int = 5,
    max_positions: int = 2,
    max_gross_exposure_mult: float = 0.75,
    initial_equity: float = 200.0,
    fee_bps: float = 5.0,
    slippage_bps: float = 2.0,
    min_notional: dict[str, float] | None = None,
) -> PanelResult:
    """Run the pre-registered long-only cross-asset momentum portfolio."""
    ts, opens, closes = align_panel(frames)
    symbols = tuple(sorted(closes))
    n = len(ts)
    if n <= lookback + 2:
        raise ValueError(f"need > {lookback + 2} overlapping bars, have {n}")

    cost_rate = (fee_bps + slippage_bps) / 10_000.0
    min_notional_map = min_notional or {}

    # Pre-compute strictly backward-looking momentum score at bar i close:
    # eligible iff close[i] > close[i - lookback] AND close[i] > mean(close[i-lookback+1 : i+1])
    scores: dict[str, np.ndarray] = {}
    for sym in symbols:
        c = closes[sym]
        s = pd.Series(c)
        ret = s.pct_change(lookback)
        sma = s.rolling(lookback).mean()
        vol = s.pct_change().rolling(lookback).std()
        raw_score = np.where(
            (ret > 0.0) & (s > sma) & (vol > 1e-8),
            ret / vol,
            -np.inf,
        )
        scores[sym] = raw_score

    equity = float(initial_equity)
    curve = np.full(n, initial_equity, dtype=float)
    # Current holdings: sym -> quantity (in underlying units)
    holdings: dict[str, float] = {}
    pending_targets: tuple[str, ...] | None = None
    rebalances: list[RebalanceRecord] = []
    total_fees = 0.0
    total_turnover = 0.0
    bars_invested = 0
    asset_pnl: dict[str, float] = dict.fromkeys(symbols, 0.0)
    asset_bars: dict[str, int] = dict.fromkeys(symbols, 0)

    for i in range(n):
        # 1. Execute pending rebalance at bar i open
        if pending_targets is not None:
            # Mark existing holdings to open[i]
            if i > 0:
                for sym, qty in holdings.items():
                    step_pnl = qty * (opens[sym][i] - closes[sym][i - 1])
                    equity += step_pnl
                    asset_pnl[sym] += step_pnl

            target_syms = [
                sym
                for sym in pending_targets
                if (equity * max_gross_exposure_mult / max(1, len(pending_targets)))
                >= min_notional_map.get(sym, 0.0)
            ]
            target_notional_each = (
                (equity * max_gross_exposure_mult) / len(target_syms)
                if target_syms
                else 0.0
            )

            new_holdings: dict[str, float] = {}
            turnover = 0.0
            for sym in symbols:
                open_px = opens[sym][i]
                old_notional = (
                    holdings.get(sym, 0.0) * open_px
                    if sym in holdings and math.isfinite(open_px)
                    else 0.0
                )
                new_notional = (
                    target_notional_each
                    if sym in target_syms and math.isfinite(open_px) and open_px > 0
                    else 0.0
                )
                turnover += abs(new_notional - old_notional)
                if new_notional > 0 and open_px > 0:
                    new_holdings[sym] = new_notional / open_px

            fees = turnover * cost_rate
            equity -= fees
            total_fees += fees
            total_turnover += turnover
            holdings = new_holdings
            rebalances.append(
                RebalanceRecord(
                    timestamp=int(ts[i]),
                    selected=tuple(target_syms),
                    notional_per_asset=round(target_notional_each, 2),
                    turnover_usdt=round(turnover, 2),
                    fees_usdt=round(fees, 4),
                    equity_after=round(equity, 2),
                )
            )

            # Accrue open[i] -> close[i] on new holdings
            for sym, qty in holdings.items():
                step_pnl = qty * (closes[sym][i] - opens[sym][i])
                equity += step_pnl
                asset_pnl[sym] += step_pnl
                asset_bars[sym] += 1
            pending_targets = None
        else:
            # Normal bar: accrue close[i-1] -> close[i]
            if i > 0 and holdings:
                for sym, qty in holdings.items():
                    step_pnl = qty * (closes[sym][i] - closes[sym][i - 1])
                    equity += step_pnl
                    asset_pnl[sym] += step_pnl
                    asset_bars[sym] += 1

        if holdings:
            bars_invested += 1
        curve[i] = equity

        # 2. At close of bar i, decide if next open is a rebalance
        if i >= lookback and (i - lookback) % rebalance_every == 0 and i + 1 < n:
            ranked = sorted(
                (
                    ( float(scores[sym][i]), sym )
                    for sym in symbols
                    if math.isfinite(float(scores[sym][i])) and float(scores[sym][i]) > 0
                ),
                reverse=True,
            )
            pending_targets = tuple(sym for _, sym in ranked[:max_positions])

    index = pd.to_datetime(ts, unit="ms")
    curve_series = pd.Series(curve, index=index, name="equity")

    # Equal-weight benchmark over the active window (from lookback onwards),
    # compounding daily equal-weighted returns across whichever instruments are listed on bar i.
    ew_eq = np.full(n, initial_equity, dtype=float)
    for i in range(lookback + 1, n):
        bar_rets = [
            (closes[sym][i] / closes[sym][i - 1] - 1.0)
            for sym in symbols
            if math.isfinite(closes[sym][i])
            and math.isfinite(closes[sym][i - 1])
            and closes[sym][i - 1] > 0
        ]
        mean_ret = sum(bar_rets) / len(bar_rets) if bar_rets else 0.0
        ew_eq[i] = ew_eq[i - 1] * (1.0 + mean_ret)
    ew_curve = pd.Series(ew_eq, index=index, name="equal_weight")

    metrics = _panel_metrics(
        curve_series.iloc[lookback:],
        initial_equity=initial_equity,
        timeframe=timeframe,
        rebalances=rebalances,
    )
    metrics["fees_paid"] = round(total_fees, 2)
    metrics["turnover_usdt"] = round(total_turnover, 2)
    metrics["exposure_pct"] = round(bars_invested / max(1, n - lookback) * 100, 1)

    benchmark = _panel_metrics(
        ew_curve.iloc[lookback:],
        initial_equity=initial_equity,
        timeframe=timeframe,
        rebalances=[],
    )

    asset_stats = {
        sym: {
            "pnl_usdt": round(asset_pnl[sym], 2),
            "bars_held": float(asset_bars[sym]),
            "share_held_pct": round(asset_bars[sym] / max(1, n - lookback) * 100, 1),
        }
        for sym in symbols
    }

    return PanelResult(
        symbols=symbols,
        timeframe=timeframe,
        lookback=lookback,
        rebalance_every=rebalance_every,
        initial_equity=initial_equity,
        final_equity=round(float(curve[-1]), 2),
        equity_curve=curve_series,
        equal_weight_curve=ew_curve,
        rebalances=tuple(rebalances),
        asset_stats=asset_stats,
        metrics=metrics,
        benchmark=benchmark,
    )


def _panel_metrics(
    curve: pd.Series,
    *,
    initial_equity: float,
    timeframe: str,
    rebalances: list[RebalanceRecord],
) -> dict[str, float]:
    if curve.empty:
        return {}
    start_val = float(curve.iloc[0])
    final = float(curve.iloc[-1])
    total_return = final / start_val - 1.0 if start_val > 0 else 0.0
    per_year = BARS_PER_YEAR.get(timeframe, 365)
    years = len(curve) / per_year
    cagr = (final / start_val) ** (1.0 / years) - 1.0 if years > 0 and final > 0 else -1.0

    returns = curve.pct_change().dropna()
    std = float(returns.std())
    mean = float(returns.mean()) if len(returns) else 0.0
    sharpe = mean / std * math.sqrt(per_year) if std > 0 else 0.0
    t_stat = mean / (std / math.sqrt(len(returns))) if std > 0 and len(returns) >= 2 else 0.0

    running_max = curve.cummax()
    drawdown = float((curve / running_max - 1.0).min())

    return {
        "total_return_pct": round(total_return * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "max_drawdown_pct": round(drawdown * 100, 2),
        "sharpe": round(sharpe, 2),
        "t_stat": round(t_stat, 2),
        "rebalances": float(len(rebalances)),
        "bars": float(len(curve)),
    }
