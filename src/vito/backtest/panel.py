"""Cross-asset panel backtester (Time-Series + Cross-Sectional Momentum).

Why a panel instead of single-series grid search
------------------------------------------------
Single-asset walk-forward on 730 daily bars produced 10 trades — too few
observations to separate signal from noise, and 84 parameter combinations
guaranteed selection bias.

This module tests **one pre-registered, fixed-parameter hypothesis** across a
macro index + monetary cross-asset panel (Crypto + US Equity Indices +
Precious Metals):

1. **Absolute trend filter (time-series momentum):** At each rebalance close
   ``t``, an instrument is eligible (long-only) only if its trailing
   ``lookback`` return is positive and its close is above its ``lookback``
   moving average. When everything is in a downtrend, the portfolio holds
   USDT cash.
2. **Relative strength rank (cross-sectional momentum):** Eligible instruments
   are ranked by volatility-adjusted return (``return / volatility`` over
   ``lookback``). The top ``max_positions`` are selected.
3. **Risk-based sizing & intrabar ATR stops:** Each selected position is sized
   by ``(equity * risk_per_trade_pct) / stop_distance``, clamped by the
   portfolio gross exposure cap (``max_gross_exposure_mult``). Between
   rebalances, every held asset carries a trailing ATR stop checked against
   each bar's ``low`` (filling at ``min(open, stop)`` on a gap-through).
4. **Next-open execution & turnover-only costs:** Weights decided at close ``t``
   execute at open ``t + 1``. Taker fees and slippage are charged on the
   notional delta traded.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from vito.strategy.indicators import atr

DEFAULT_BASKET: tuple[str, ...] = (
    "BTC-USDT",
    "ETH-USDT",
    "SOL-USDT",
    "PAXG-USDT",
    "QQQ-USDT-SWAP",
    "SPY-USDT-SWAP",
    "XAG-USDT-SWAP",
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
    *,
    atr_window: int = 14,
    atr_mult: float = 2.5,
    min_stop_pct: float = 0.006,
) -> tuple[
    np.ndarray,
    dict[str, np.ndarray],
    dict[str, np.ndarray],
    dict[str, np.ndarray],
    dict[str, np.ndarray],
    dict[str, np.ndarray],
]:
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
    highs: dict[str, np.ndarray] = {}
    lows: dict[str, np.ndarray] = {}
    closes: dict[str, np.ndarray] = {}
    stop_dists: dict[str, np.ndarray] = {}

    for sym, df in non_empty.items():
        clean = df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
        c_ser = clean["close"].astype(float)
        if "high" in clean.columns and "low" in clean.columns:
            atr_ser = atr(
                clean["high"].astype(float),
                clean["low"].astype(float),
                c_ser,
                atr_window,
            )
            raw_stop = (atr_ser * atr_mult / c_ser).clip(lower=min_stop_pct)
        else:
            raw_stop = pd.Series(0.02, index=clean.index)
        clean = clean.assign(_stop_dist=raw_stop)

        indexed = clean.set_index("timestamp").reindex(ts)
        opens[sym] = indexed["open"].to_numpy(dtype=float)
        highs[sym] = (
            indexed["high"].to_numpy(dtype=float)
            if "high" in indexed.columns
            else opens[sym].copy()
        )
        lows[sym] = (
            indexed["low"].to_numpy(dtype=float)
            if "low" in indexed.columns
            else opens[sym].copy()
        )
        closes[sym] = indexed["close"].to_numpy(dtype=float)
        stop_dists[sym] = indexed["_stop_dist"].fillna(0.02).to_numpy(dtype=float)

    return ts, opens, highs, lows, closes, stop_dists


def rank_panel_at(
    frames: dict[str, pd.DataFrame],
    *,
    lookback: int = 20,
    max_positions: int = 2,
    atr_mult: float = 2.5,
) -> list[tuple[str, float, float, float]]:
    """Rank instruments on the latest closed bar.

    Returns ``[(inst_id, score, close, stop_distance), ...]`` for the top
    ``max_positions`` instruments passing the absolute trend filter.
    """
    _, _, _, _, closes, stop_dists = align_panel(frames, atr_mult=atr_mult)
    symbols = sorted(closes)
    candidates: list[tuple[float, str, float, float]] = []
    for sym in symbols:
        s = pd.Series(closes[sym]).dropna()
        if len(s) <= lookback:
            continue
        ret = float(s.pct_change(lookback).iloc[-1])
        sma = float(s.rolling(lookback).mean().iloc[-1])
        vol = float(s.pct_change().rolling(lookback).std().iloc[-1])
        last_close = float(s.iloc[-1])
        stop_d = float(stop_dists[sym][-1])
        if (
            math.isfinite(ret)
            and math.isfinite(vol)
            and ret > 0
            and last_close > sma
            and vol > 1e-8
        ):
            candidates.append((ret / vol, sym, last_close, stop_d))
    candidates.sort(reverse=True)
    return [
        (sym, round(score, 4), last_close, round(stop_d, 4))
        for score, sym, last_close, stop_d in candidates[:max_positions]
    ]


def run_panel_momentum(
    frames: dict[str, pd.DataFrame],
    *,
    timeframe: str = "1d",
    lookback: int = 20,
    rebalance_every: int = 5,
    max_positions: int = 2,
    max_gross_exposure_mult: float = 1.50,
    risk_per_trade_pct: float = 3.0,
    atr_mult: float = 2.5,
    use_stops: bool = True,
    initial_equity: float = 200.0,
    fee_bps: float = 5.0,
    slippage_bps: float = 2.0,
    cash_apr_pct: float = 0.0,
    min_notional: dict[str, float] | None = None,
) -> PanelResult:
    """Run the pre-registered long-only cross-asset momentum portfolio."""
    ts, opens, _, lows, closes, stop_dists = align_panel(frames, atr_mult=atr_mult)
    symbols = tuple(sorted(closes))
    n = len(ts)
    if n <= lookback + 2:
        raise ValueError(f"need > {lookback + 2} overlapping bars, have {n}")

    cost_rate = (fee_bps + slippage_bps) / 10_000.0
    slip_rate = slippage_bps / 10_000.0
    fee_rate = fee_bps / 10_000.0
    risk_frac = risk_per_trade_pct / 100.0
    per_year = BARS_PER_YEAR.get(timeframe, 365)
    cash_bar_rate = (cash_apr_pct / 100.0) / per_year if cash_apr_pct > 0 else 0.0
    min_notional_map = min_notional or {}

    # Pre-compute strictly backward-looking momentum score at bar i close
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
    holdings: dict[str, float] = {}
    stops: dict[str, float] = {}
    pending_targets: tuple[tuple[str, float], ...] | None = None
    rebalances: list[RebalanceRecord] = []
    total_fees = 0.0
    total_turnover = 0.0
    total_cash_yield = 0.0
    stops_hit = 0
    bars_invested = 0
    asset_pnl: dict[str, float] = dict.fromkeys(symbols, 0.0)
    asset_bars: dict[str, int] = dict.fromkeys(symbols, 0)

    def check_intrabar_stops(i: int, from_open: bool) -> None:
        nonlocal equity, total_fees, total_turnover, stops_hit
        if not use_stops or not holdings:
            return
        for sym in list(holdings):
            qty = holdings[sym]
            lo_px = lows[sym][i]
            open_px = opens[sym][i]
            ref_px = open_px if from_open else closes[sym][i - 1]
            stop_px = stops.get(sym, 0.0)
            if math.isfinite(lo_px) and stop_px > 0 and lo_px <= stop_px:
                fill_px = min(open_px, stop_px) * (1.0 - slip_rate)
                pnl = qty * (fill_px - ref_px)
                notional_exit = qty * fill_px
                fee = notional_exit * fee_rate
                equity += pnl - fee
                asset_pnl[sym] += pnl
                total_fees += fee
                total_turnover += notional_exit
                stops_hit += 1
                del holdings[sym]
                stops.pop(sym, None)

    for i in range(n):
        # 1. Execute pending rebalance at bar i open
        if pending_targets is not None:
            # First mark existing holdings from close[i-1] to open[i]
            if i > 0:
                for sym, qty in list(holdings.items()):
                    open_px = opens[sym][i]
                    prev_c = closes[sym][i - 1]
                    if math.isfinite(open_px) and math.isfinite(prev_c):
                        # If gapped below stop at open, exit at open
                        if use_stops and open_px <= stops.get(sym, 0.0):
                            fill_px = open_px * (1.0 - slip_rate)
                            pnl = qty * (fill_px - prev_c)
                            notional_exit = qty * fill_px
                            fee = notional_exit * fee_rate
                            equity += pnl - fee
                            asset_pnl[sym] += pnl
                            total_fees += fee
                            total_turnover += notional_exit
                            stops_hit += 1
                            del holdings[sym]
                            stops.pop(sym, None)
                        else:
                            step_pnl = qty * (open_px - prev_c)
                            equity += step_pnl
                            asset_pnl[sym] += step_pnl

            gross_cap_each = (
                (equity * max_gross_exposure_mult) / max(1, len(pending_targets))
                if pending_targets
                else 0.0
            )
            desired_notional: dict[str, float] = {}
            desired_stop_dist: dict[str, float] = {}
            for sym, stop_d in pending_targets:
                safe_stop = max(0.006, stop_d if math.isfinite(stop_d) and stop_d > 0 else 0.02)
                risk_notional = (equity * risk_frac) / safe_stop if use_stops else gross_cap_each
                alloc = min(gross_cap_each, risk_notional)
                if alloc >= min_notional_map.get(sym, 0.0):
                    desired_notional[sym] = alloc
                    desired_stop_dist[sym] = safe_stop

            new_holdings: dict[str, float] = {}
            new_stops: dict[str, float] = {}
            turnover = 0.0
            for sym in symbols:
                open_px = opens[sym][i]
                old_notional = (
                    holdings.get(sym, 0.0) * open_px
                    if sym in holdings and math.isfinite(open_px)
                    else 0.0
                )
                new_notional = (
                    desired_notional.get(sym, 0.0)
                    if sym in desired_notional and math.isfinite(open_px) and open_px > 0
                    else 0.0
                )
                turnover += abs(new_notional - old_notional)
                if new_notional > 0 and open_px > 0:
                    new_holdings[sym] = new_notional / open_px
                    stop_level = open_px * (1.0 - desired_stop_dist[sym])
                    if sym in stops and sym in holdings:
                        new_stops[sym] = max(stops[sym], stop_level)
                    else:
                        new_stops[sym] = stop_level

            fees = turnover * cost_rate
            equity -= fees
            total_fees += fees
            total_turnover += turnover
            holdings = new_holdings
            stops = new_stops
            avg_alloc = (
                sum(desired_notional.values()) / len(desired_notional)
                if desired_notional
                else 0.0
            )
            rebalances.append(
                RebalanceRecord(
                    timestamp=int(ts[i]),
                    selected=tuple(desired_notional),
                    notional_per_asset=round(avg_alloc, 2),
                    turnover_usdt=round(turnover, 2),
                    fees_usdt=round(fees, 4),
                    equity_after=round(equity, 2),
                )
            )

            # Check intrabar stops during bar i (from open[i])
            check_intrabar_stops(i, from_open=True)

            # Accrue open[i] -> close[i] on surviving holdings and ratchet trailing stop
            for sym, qty in holdings.items():
                step_pnl = qty * (closes[sym][i] - opens[sym][i])
                equity += step_pnl
                asset_pnl[sym] += step_pnl
                asset_bars[sym] += 1
                sd = stop_dists[sym][i]
                if math.isfinite(sd) and sd > 0:
                    stops[sym] = max(stops.get(sym, 0.0), closes[sym][i] * (1.0 - sd))
            pending_targets = None
        else:
            # Normal bar: check intrabar stops first, then accrue close[i-1] -> close[i]
            if i > 0 and holdings:
                check_intrabar_stops(i, from_open=False)
                for sym, qty in holdings.items():
                    step_pnl = qty * (closes[sym][i] - closes[sym][i - 1])
                    equity += step_pnl
                    asset_pnl[sym] += step_pnl
                    asset_bars[sym] += 1
                    sd = stop_dists[sym][i]
                    if math.isfinite(sd) and sd > 0:
                        stops[sym] = max(stops.get(sym, 0.0), closes[sym][i] * (1.0 - sd))

        if holdings:
            bars_invested += 1
        if i >= lookback and cash_bar_rate > 0:
            deployed = sum(
                qty * closes[sym][i]
                for sym, qty in holdings.items()
                if math.isfinite(closes[sym][i]) and closes[sym][i] > 0
            )
            idle_cash = max(0.0, equity - deployed)
            cy = idle_cash * cash_bar_rate
            equity += cy
            total_cash_yield += cy
        curve[i] = equity

        # 2. At close of bar i, decide if next open is a rebalance
        if i >= lookback and (i - lookback) % rebalance_every == 0 and i + 1 < n:
            ranked = sorted(
                (
                    (float(scores[sym][i]), sym, float(stop_dists[sym][i]))
                    for sym in symbols
                    if math.isfinite(float(scores[sym][i])) and float(scores[sym][i]) > 0
                ),
                reverse=True,
            )
            pending_targets = tuple((sym, sd) for _, sym, sd in ranked[:max_positions])

    index = pd.to_datetime(ts, unit="ms")
    curve_series = pd.Series(curve, index=index, name="equity")

    # Equal-weight benchmark over the active window (from lookback onwards)
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
    metrics["stops_hit"] = float(stops_hit)
    metrics["cash_yield_usdt"] = round(total_cash_yield, 2)

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
