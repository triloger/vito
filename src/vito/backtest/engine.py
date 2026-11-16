"""Event-driven backtester.

Rules that make the output trustworthy:

* **No lookahead.** Signals are computed on a bar's close and executed at the
  *next* bar's open. Stops are checked intrabar and fill at the stop, or at the
  open if the bar gapped through it.
* **Same sizing as live.** Every entry goes through ``vito.risk.size_position``
  with the shipped policy, so a backtest cannot be more aggressive than the
  executor.
* **Costs are paid.** Taker fees on both legs, slippage on both legs, and
  funding every interval while a perpetual position is open.
* **Refusals are counted.** If the policy blocks a trade (stop too tight, gross
  cap, instrument), it is logged rather than silently dropped.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from statistics import NormalDist

import numpy as np
import pandas as pd

from vito.policy import Policy
from vito.risk import SizeRequest, size_position

BARS_PER_YEAR = {"1h": 24 * 365, "4h": 6 * 365, "1d": 365, "15m": 96 * 365, "5m": 288 * 365}
BAR_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
_NORM = NormalDist()


@dataclass(frozen=True, slots=True)
class Trade:
    side: str
    entry_ts: int
    exit_ts: int
    entry_price: float
    exit_price: float
    notional: float
    qty: float
    stop_price: float
    pnl: float
    fees: float
    funding: float
    risk_amount: float
    bars: int
    exit_reason: str

    @property
    def net(self) -> float:
        return self.pnl - self.fees - self.funding

    @property
    def r_multiple(self) -> float:
        return self.net / self.risk_amount if self.risk_amount else 0.0


@dataclass(frozen=True, slots=True)
class BacktestResult:
    inst_id: str
    strategy: str
    timeframe: str
    initial_equity: float
    final_equity: float
    equity_curve: pd.Series
    trades: tuple[Trade, ...]
    metrics: dict[str, float] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)

    @property
    def n_trades(self) -> int:
        return len(self.trades)


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------


def run_backtest(
    candles: pd.DataFrame,
    signals: pd.DataFrame,
    *,
    inst_id: str,
    strategy_name: str,
    policy: Policy,
    initial_equity: float = 200.0,
    fee_bps: float = 5.0,
    slippage_bps: float = 2.0,
    funding: pd.Series | None = None,
    timeframe: str = "4h",
    max_bars: int = 90,
    allow_shorts: bool = True,
    min_notional: float = 0.0,
) -> BacktestResult:
    if len(candles) != len(signals):
        raise ValueError("candles and signals must be aligned")

    ts = candles["timestamp"].to_numpy(dtype=np.int64)
    o = candles["open"].to_numpy(dtype=float)
    h = candles["high"].to_numpy(dtype=float)
    lo = candles["low"].to_numpy(dtype=float)
    c = candles["close"].to_numpy(dtype=float)

    sides = signals["side"].fillna("flat").to_numpy(dtype=object)
    stops = signals["stop_distance"].to_numpy(dtype=float)
    conviction = signals["conviction"].fillna(False).to_numpy(dtype=bool)

    fee_rate = fee_bps / 10_000.0
    slip = slippage_bps / 10_000.0
    funding_by_bar = _align_funding(funding, ts, BAR_MS.get(timeframe))

    n = len(candles)
    equity = float(initial_equity)
    peak = equity
    curve = np.full(n, initial_equity, dtype=float)

    pos: dict | None = None
    pending_entry: tuple[str, float, bool] | None = None
    pending_exit = False

    trades: list[Trade] = []
    skipped: Counter[str] = Counter()
    bars_in_market = 0
    total_fees = 0.0
    total_funding = 0.0

    def close_position(i: int, price: float, reason: str) -> None:
        nonlocal pos, equity, total_fees, total_funding
        assert pos is not None
        sign = 1.0 if pos["side"] == "long" else -1.0
        if pos["side"] == "long":
            price *= 1 - slip
        else:
            price *= 1 + slip
        pnl = (price - pos["entry_price"]) * pos["qty"] * sign
        fees = pos["notional"] * fee_rate * 2
        equity += pnl - fees
        total_fees += fees
        total_funding += pos["funding"]
        trades.append(
            Trade(
                side=pos["side"],
                entry_ts=int(ts[pos["entry_i"]]),
                exit_ts=int(ts[i]),
                entry_price=round(pos["entry_price"], 6),
                exit_price=round(price, 6),
                notional=round(pos["notional"], 2),
                qty=round(pos["qty"], 8),
                stop_price=round(pos["stop"], 6),
                pnl=round(pnl, 4),
                fees=round(fees, 4),
                funding=round(pos["funding"], 4),
                risk_amount=round(pos["risk_amount"], 4),
                bars=i - pos["entry_i"],
                exit_reason=reason,
            )
        )
        pos = None

    for i in range(n):
        # 1. exits at this bar's open. A gap through the stop wins over a signal:
        #    the stop is a resting order, the signal is an intention.
        if pos is not None:
            gapped = (pos["side"] == "long" and o[i] <= pos["stop"]) or (
                pos["side"] == "short" and o[i] >= pos["stop"]
            )
            if gapped:
                close_position(i, o[i], "stop")
            elif pending_exit:
                close_position(i, o[i], "signal")
        pending_exit = False

        # 2. pending entry executes at this bar's open
        if pos is None and pending_entry is not None:
            side, stop_distance, convict = pending_entry
            price = o[i] * (1 + slip) if side == "long" else o[i] * (1 - slip)
            request = SizeRequest(
                equity=equity,
                instrument=inst_id,
                stop_distance=stop_distance,
                conviction=convict,
                open_positions=0,
                gross_exposure=0.0,
                min_notional=min_notional,
            )
            result = size_position(request, policy)
            if result.ok:
                pos = {
                    "side": side,
                    "entry_i": i,
                    "entry_price": price,
                    "qty": result.notional / price,
                    "notional": result.notional,
                    "stop": price * (1 - stop_distance)
                    if side == "long"
                    else price * (1 + stop_distance),
                    "risk_amount": result.risk_amount,
                    "funding": 0.0,
                }
            else:
                skipped[str(result.reason)] += 1
        pending_entry = None

        # 3. intrabar stop, funding, and time stop
        if pos is not None:
            bars_in_market += 1
            sign = 1.0 if pos["side"] == "long" else -1.0

            rate = funding_by_bar[i]
            if rate:
                flow = -sign * rate * pos["notional"]
                equity += flow
                pos["funding"] += -flow

            hit = (pos["side"] == "long" and lo[i] <= pos["stop"]) or (
                pos["side"] == "short" and h[i] >= pos["stop"]
            )
            if hit:
                # gap-through fills at the open, not at the stop
                fill = min(o[i], pos["stop"]) if pos["side"] == "long" else max(o[i], pos["stop"])
                close_position(i, fill, "stop")
            elif i - pos["entry_i"] >= max_bars:
                close_position(i, c[i], "max_bars")

        # 4. mark to market
        unrealised = 0.0
        if pos is not None:
            sign = 1.0 if pos["side"] == "long" else -1.0
            unrealised = (c[i] - pos["entry_price"]) * pos["qty"] * sign
        curve[i] = equity + unrealised
        peak = max(peak, curve[i])

        # 5. decide next bar's action, from this bar's close
        if pos is not None:
            wanted = sides[i]
            if wanted == "flat" or (wanted != "flat" and _opposite(wanted, pos["side"])):
                pending_exit = True
        elif i + 1 < n:
            wanted = sides[i]
            if wanted in ("long", "short"):
                if wanted == "short" and not allow_shorts:
                    pass
                elif not math.isfinite(stops[i]) or stops[i] <= 0:
                    skipped["invalid_stop"] += 1
                else:
                    pending_entry = (wanted, float(stops[i]), bool(conviction[i]))

    if pos is not None and n:
        close_position(n - 1, c[n - 1], "end")
        # the forced close paid fees and slippage; the last mark did not
        curve[n - 1] = equity

    curve_series = pd.Series(curve, index=pd.to_datetime(ts, unit="ms"), name="equity")
    metrics = compute_metrics(curve_series, trades, initial_equity, timeframe)
    metrics["fees_paid"] = round(total_fees, 2)
    metrics["funding_paid"] = round(total_funding, 2)
    metrics["exposure_pct"] = round(bars_in_market / n * 100, 1) if n else 0.0

    return BacktestResult(
        inst_id=inst_id,
        strategy=strategy_name,
        timeframe=timeframe,
        initial_equity=float(initial_equity),
        final_equity=round(float(curve[-1]) if n else initial_equity, 2),
        equity_curve=curve_series,
        trades=tuple(trades),
        metrics=metrics,
        skipped=dict(skipped),
    )


def _opposite(a: str, b: str) -> bool:
    return (a, b) in (("long", "short"), ("short", "long"))


def _align_funding(
    funding: pd.Series | None, ts: np.ndarray, bar_ms: int | None = None
) -> np.ndarray:
    """Assign each funding settlement to the candle ``[ts[i], ts[i+1])`` it falls in.

    Settlements strictly before ``ts[0]`` or at/after the end of the final bar
    are ignored rather than clamped onto the first or last bar.
    """
    out = np.zeros(len(ts), dtype=float)
    if funding is None or len(funding) == 0 or len(ts) == 0:
        return out
    step = bar_ms or (int(ts[1] - ts[0]) if len(ts) > 1 else 86_400_000)
    end_ts = int(ts[-1]) + step
    fts = np.asarray(funding.index, dtype=np.int64)
    rates = np.asarray(funding.to_numpy(), dtype=float)
    idx = np.searchsorted(ts, fts, side="right") - 1
    for k, i in enumerate(idx):
        if 0 <= i < len(ts) and ts[0] <= fts[k] < end_ts:
            out[i] += rates[k]
    return out


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def deflated_sharpe(
    sharpe: float, n_bars: int, n_trials: int, per_year: int = 365
) -> float:
    """Probability that ``sharpe`` exceeds the expected null max across ``n_trials``.

    Uses the Bailey & López de Prado (2014) expected maximum Sharpe approximation
    under independent Gaussian trials of length ``years = n_bars / per_year``.
    """
    if n_bars <= 2 or per_year <= 0:
        return 0.0
    years = n_bars / per_year
    if years <= 0:
        return 0.0
    se_sr = math.sqrt((1.0 + 0.5 * (sharpe**2) / per_year) / years)
    if se_sr <= 0:
        return 0.0
    if n_trials <= 1:
        sr0 = 0.0
    else:
        gamma = 0.5772156649015329
        z1 = _NORM.inv_cdf(1.0 - 1.0 / n_trials)
        z2 = _NORM.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
        z_max = (1.0 - gamma) * z1 + gamma * z2
        sr0 = z_max / math.sqrt(years)
    z = (sharpe - sr0) / se_sr
    return round(float(_NORM.cdf(z)), 4)


def buy_and_hold(
    candles: pd.DataFrame, *, initial_equity: float = 200.0, timeframe: str = "4h"
) -> dict[str, float]:
    """The benchmark a strategy has to beat on a risk-adjusted basis."""
    close = candles["close"].astype(float).reset_index(drop=True)
    if close.empty:
        return {}
    index = pd.to_datetime(candles["timestamp"], unit="ms")
    curve = pd.Series((initial_equity * close / close.iloc[0]).to_numpy(), index=index)
    return compute_metrics(curve, [], initial_equity, timeframe)


def compute_metrics(
    curve: pd.Series,
    trades: list[Trade],
    initial: float,
    timeframe: str,
    *,
    n_trials: int = 1,
) -> dict[str, float]:
    if len(curve) == 0:
        return {}

    final = float(curve.iloc[-1])
    total_return = final / initial - 1
    per_year = BARS_PER_YEAR.get(timeframe, 6 * 365)
    years = len(curve) / per_year
    cagr = (final / initial) ** (1 / years) - 1 if years > 0 and final > 0 else -1.0

    returns = curve.pct_change().dropna()
    std = float(returns.std())
    sharpe = float(returns.mean()) / std * math.sqrt(per_year) if std > 0 else 0.0

    running_max = curve.cummax()
    drawdown = (curve / running_max - 1).min()

    wins = [t for t in trades if t.net > 0]
    losses = [t for t in trades if t.net <= 0]
    gross_profit = sum(t.net for t in wins)
    gross_loss = -sum(t.net for t in losses)
    r_mults = [t.r_multiple for t in trades]
    avg_r = sum(r_mults) / len(r_mults) if r_mults else 0.0
    if len(r_mults) >= 2:
        r_std = float(np.std(r_mults, ddof=1))
        t_stat = avg_r / (r_std / math.sqrt(len(r_mults))) if r_std > 0 else 0.0
    else:
        t_stat = 0.0

    dsr = deflated_sharpe(sharpe, len(curve), max(1, n_trials), per_year)

    return {
        "total_return_pct": round(total_return * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "max_drawdown_pct": round(float(drawdown) * 100, 2),
        "sharpe": round(sharpe, 2),
        "deflated_sharpe": dsr,
        "t_stat": round(t_stat, 2),
        "trades": float(len(trades)),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "expectancy_r": round(avg_r, 3),
        "avg_bars_held": round(sum(t.bars for t in trades) / len(trades), 1) if trades else 0.0,
    }
