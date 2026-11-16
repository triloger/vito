"""Paper broker — the same rules, against live prices, with no capital.

Why a *step* and not a daemon
-----------------------------
The broker is deliberately a single idempotent evaluation, meant to be driven by
cron or a timer. It reads the newest **closed** candles, advances the persisted
position over any bars it has not seen, and writes state back atomically. Running
it twice in the same minute does nothing the second time. A long-running process
would add reconnect logic, in-memory state and a way to be silently dead; a step
that fails is a step you can see failing.

What this does model
--------------------
* Bar-close decisions, next-bar-open fills — no lookahead.
* Taker fees and slippage on both legs.
* Intrabar stops, including gapping through the stop and filling at the open.
* The policy's sizing function, so paper cannot take a position the live router
  would refuse.
* Catch-up: if the timer missed three bars, all three are applied in order.

What this does **not** model
----------------------------
* Funding payments. The backtester charges them; paper does not yet.
* Spread widening, partial fills, and order rejection.
* Latency between the close and the fill.

Those are the things shadow mode against a live account exists to measure. Paper
is for proving the plumbing, not for estimating returns.

The equivalence that matters: over the same bars, this module and
``backtest.engine`` must produce the same trades. That is asserted in
``tests/test_paper.py`` so the two implementations cannot drift apart.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from vito.backtest.engine import Trade
from vito.policy import Policy
from vito.risk import SizeRequest, kill_switch_engaged, size_position
from vito.strategy import build

STATE_VERSION = 1
DEFAULT_STATE_DIR = Path.home() / ".vito"


class PaperError(RuntimeError):
    """Raised when paper state cannot be read or is from another version."""


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PaperState:
    """Everything needed to resume. Written to disk after every step."""

    version: int
    inst_id: str
    strategy: str
    timeframe: str
    params: dict[str, Any]
    initial_equity: float
    equity: float
    peak_equity: float
    last_bar_ts: int
    position: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    fees_paid: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PaperState:
        if int(data.get("version", 0)) != STATE_VERSION:
            raise PaperError(
                f"paper state version {data.get('version')!r} is not {STATE_VERSION}; "
                "delete the file to start a new run"
            )
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def trade_objects(self) -> list[Trade]:
        return [Trade(**t) for t in self.trades]

    def summary(self) -> dict[str, Any]:
        trades = self.trade_objects
        wins = sum(1 for t in trades if t.net > 0)
        return_pct = (
            (self.equity / self.initial_equity - 1) * 100 if self.initial_equity else 0.0
        )
        return {
            "inst_id": self.inst_id,
            "strategy": self.strategy,
            "timeframe": self.timeframe,
            "initial_equity": round(self.initial_equity, 2),
            "equity": round(self.equity, 2),
            "return_pct": round(return_pct, 2),
            "peak_equity": round(self.peak_equity, 2),
            "trades": len(trades),
            "win_rate_pct": round(wins / len(trades) * 100, 1) if trades else 0.0,
            "net_pnl": round(sum(t.net for t in trades), 2),
            "fees_paid": round(self.fees_paid, 2),
            "in_position": self.position is not None,
            "position": self.position,
            "last_bar_ts": self.last_bar_ts,
        }


def state_path_for(
    inst_id: str, strategy: str, timeframe: str, directory: Path | None = None
) -> Path:
    directory = Path(directory) if directory else DEFAULT_STATE_DIR
    slug = f"{inst_id}_{strategy}_{timeframe}".replace("/", "-")
    return directory / f"paper-{slug}.json"


def load_state(path: Path) -> PaperState | None:
    if not path.exists():
        return None
    try:
        return PaperState.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise PaperError(f"{path} is not valid JSON: {exc}") from exc


def save_state(path: Path, state: PaperState) -> None:
    """Atomic write: a crash mid-save must not truncate the track record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state.to_dict(), indent=2, sort_keys=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".paper-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepReport:
    status: str  # warmed_up | no_new_bar | stepped
    bars_processed: int
    actions: tuple[str, ...]
    marked_equity: float
    state: PaperState

    @property
    def changed(self) -> bool:
        return bool(self.actions)


@dataclass(slots=True)
class _Pending:
    """A decision made at one close, to be executed at the next open."""

    entry: tuple[str, float, bool] | None = None
    exit: bool = False


# ---------------------------------------------------------------------------
# broker
# ---------------------------------------------------------------------------


class PaperBroker:
    """Applies the engine's rules to one new batch of bars at a time."""

    def __init__(
        self,
        *,
        policy: Policy,
        inst_id: str,
        strategy: str,
        timeframe: str,
        initial_equity: float = 200.0,
        fee_bps: float = 5.0,
        slippage_bps: float = 2.0,
        max_bars: int = 90,
        params: dict[str, Any] | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.policy = policy
        self.inst_id = inst_id
        self.strategy = strategy
        self.timeframe = timeframe
        self.initial_equity = float(initial_equity)
        self.fee_rate = fee_bps / 10_000.0
        self.slip = slippage_bps / 10_000.0
        self.max_bars = max_bars
        self.params = params or {}
        self.state_path = state_path or state_path_for(inst_id, strategy, timeframe)
        self._signal_cache: dict[int, pd.DataFrame] = {}
        self._halted = False

    # -- state ------------------------------------------------------------

    def state(self) -> PaperState:
        existing = load_state(self.state_path)
        if existing is not None:
            return existing
        return PaperState(
            version=STATE_VERSION,
            inst_id=self.inst_id,
            strategy=self.strategy,
            timeframe=self.timeframe,
            params=self.params,
            initial_equity=self.initial_equity,
            equity=self.initial_equity,
            peak_equity=self.initial_equity,
            last_bar_ts=0,
        )

    def reset(self) -> Path:
        if self.state_path.exists():
            self.state_path.unlink()
        return self.state_path

    # -- step -------------------------------------------------------------

    def step(
        self,
        candles: pd.DataFrame,
        *,
        replay: bool = False,
        persist: bool = True,
    ) -> StepReport:
        """Advance over every closed bar newer than the persisted watermark.

        ``replay=True`` starts flat at the first bar, which is what the
        equivalence test against the backtester uses. It is not the production
        path: live paper trading warms up on history and trades only bars that
        close after it starts.
        """
        if candles.empty:
            raise PaperError("no candles")
        candles = candles.reset_index(drop=True)
        ts = candles["timestamp"].to_numpy(dtype="int64")

        self._halted = kill_switch_active(self.policy)

        state = self.state()
        if state.last_bar_ts == 0 and not replay:
            # Warm up on history, then wait for the next close. Entering on a
            # signal that was already true before the run began would be fitting
            # the past.
            state.last_bar_ts = int(ts[-1])
            if persist:
                save_state(self.state_path, state)
            return StepReport(
                "warmed_up", 0, (), self._marked(state, candles.iloc[-1]), state
            )

        signals = self._signals_for(candles, len(candles))

        if replay:
            start = 0
        else:
            newer = (ts > state.last_bar_ts).nonzero()[0]
            if len(newer) == 0:
                return StepReport(
                    "no_new_bar", 0, (), self._marked(state, candles.iloc[-1]), state
                )
            start = int(newer[0])

        actions: list[str] = []
        pending = _Pending()
        if start > 0:
            # Re-derive the decision taken at the previous close. It is a pure
            # function of that bar, so recomputation is exact.
            self._decide(start - 1, signals, state, pending)

        for i in range(start, len(candles)):
            self._apply_bar(i, candles.iloc[i], state, actions, pending)
            self._decide(i, signals, state, pending)

        state.last_bar_ts = max(state.last_bar_ts, int(ts[-1]))
        marked = self._marked(state, candles.iloc[-1])
        state.peak_equity = max(state.peak_equity, marked)

        if persist:
            save_state(self.state_path, state)

        return StepReport("stepped", len(candles) - start, tuple(actions), marked, state)

    # -- internals --------------------------------------------------------

    def _signals_for(self, candles: pd.DataFrame, n: int) -> pd.DataFrame:
        """Cached per candle count, invalidated when a new bar arrives."""
        cached = self._signal_cache.get(n)
        if cached is None:
            cached = build(self.strategy, **self.params).signals(self.inst_id, candles)
            self._signal_cache = {n: cached}
        return cached

    def _apply_bar(
        self,
        i: int,
        bar: pd.Series,
        state: PaperState,
        actions: list[str],
        pending: _Pending,
    ) -> None:
        ts = int(bar["timestamp"])
        open_, high, low, close = (
            float(bar["open"]),
            float(bar["high"]),
            float(bar["low"]),
            float(bar["close"]),
        )
        closed_now = False

        # 1. exits at this bar's open. A gap through the stop beats a signal.
        if state.position is not None:
            pos = state.position
            gapped = (pos["side"] == "long" and open_ <= pos["stop"]) or (
                pos["side"] == "short" and open_ >= pos["stop"]
            )
            if gapped:
                self._close(state, i, ts, open_, "stop", actions)
                closed_now = True
            elif pending.exit:
                self._close(state, i, ts, open_, "signal", actions)
                closed_now = True

        # 2. entry at this bar's open — a stop that fired above cancels it, and
        #    the operator's halt file blocks new risk while letting exits run.
        if state.position is None and pending.entry is not None and not closed_now:
            if self._halted:
                state.skipped["kill_switch"] = state.skipped.get("kill_switch", 0) + 1
                actions.append("halted: new entries blocked (kill switch)")
                pending.entry = None
                return
            side, stop_distance, conviction = pending.entry
            price = open_ * (1 + self.slip) if side == "long" else open_ * (1 - self.slip)
            result = size_position(
                SizeRequest(
                    equity=state.equity,
                    instrument=self.inst_id,
                    stop_distance=stop_distance,
                    conviction=conviction,
                    open_positions=0,
                    gross_exposure=0.0,
                    min_notional=0.0,
                ),
                self.policy,
            )
            if result.ok:
                stop = (
                    price * (1 - stop_distance)
                    if side == "long"
                    else price * (1 + stop_distance)
                )
                state.position = {
                    "side": side,
                    "entry_i": i,
                    "entry_ts": ts,
                    "entry_price": price,
                    "qty": result.notional / price,
                    "notional": result.notional,
                    "stop": stop,
                    "risk_amount": result.risk_amount,
                }
                actions.append(
                    f"open {side} {result.notional:.2f} USDT @ {price:.2f} "
                    f"(stop {stop:.2f})"
                )
            else:
                key = str(result.reason)
                state.skipped[key] = state.skipped.get(key, 0) + 1
                actions.append(f"refused {side}: {result.reason}")
        pending.entry = None

        # 3. intrabar stop, then the time stop.
        if state.position is not None:
            pos = state.position
            hit = (pos["side"] == "long" and low <= pos["stop"]) or (
                pos["side"] == "short" and high >= pos["stop"]
            )
            if hit:
                fill = (
                    min(open_, pos["stop"])
                    if pos["side"] == "long"
                    else max(open_, pos["stop"])
                )
                self._close(state, i, ts, fill, "stop", actions)
            elif i - pos["entry_i"] >= self.max_bars:
                self._close(state, i, ts, close, "max_bars", actions)

    def _decide(
        self, i: int, signals: pd.DataFrame, state: PaperState, pending: _Pending
    ) -> None:
        """Act on this close, at the next open — the engine's rule exactly."""
        wanted = str(signals["side"].iat[i])
        pending.entry = None
        pending.exit = False

        if state.position is not None:
            pending.exit = wanted == "flat" or (
                wanted in ("long", "short") and wanted != state.position["side"]
            )
            return

        stop = float(signals["stop_distance"].iat[i])
        if wanted in ("long", "short") and stop > 0 and stop == stop:  # not NaN
            pending.entry = (wanted, stop, bool(signals["conviction"].iat[i]))

    def _close(
        self,
        state: PaperState,
        i: int,
        ts: int,
        price: float,
        reason: str,
        actions: list[str],
    ) -> None:
        pos = state.position
        assert pos is not None
        sign = 1.0 if pos["side"] == "long" else -1.0
        fill = price * (1 - self.slip) if pos["side"] == "long" else price * (1 + self.slip)
        pnl = (fill - pos["entry_price"]) * pos["qty"] * sign
        fees = pos["notional"] * self.fee_rate * 2
        state.equity += pnl - fees
        state.fees_paid += fees

        trade = Trade(
            side=pos["side"],
            entry_ts=int(pos["entry_ts"]),
            exit_ts=ts,
            entry_price=round(float(pos["entry_price"]), 6),
            exit_price=round(fill, 6),
            notional=round(float(pos["notional"]), 2),
            qty=round(float(pos["qty"]), 8),
            stop_price=round(float(pos["stop"]), 6),
            pnl=round(pnl, 4),
            fees=round(fees, 4),
            funding=0.0,
            risk_amount=round(float(pos["risk_amount"]), 4),
            bars=i - int(pos["entry_i"]),
            exit_reason=reason,
        )
        state.trades.append(asdict(trade))
        state.position = None
        actions.append(
            f"close {trade.side} @ {fill:.2f} ({reason}) net {trade.net:+.2f}"
        )

    def _marked(self, state: PaperState, last_bar: pd.Series) -> float:
        if state.position is None:
            return state.equity
        pos = state.position
        sign = 1.0 if pos["side"] == "long" else -1.0
        unrealised = (float(last_bar["close"]) - pos["entry_price"]) * pos["qty"] * sign
        return state.equity + unrealised


def kill_switch_active(policy: Policy) -> bool:
    return kill_switch_engaged(policy.execution.kill_switch_file)
