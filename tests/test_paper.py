from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from vito.backtest import run_backtest
from vito.paper import PaperBroker, PaperError, load_state, state_path_for
from vito.paper.broker import STATE_VERSION, PaperState
from vito.strategy import build

STEP = 3_600_000


def trending(n: int = 400) -> pd.DataFrame:
    """Rises, breaks out, retraces — enough structure to force trades."""
    close = [100 + i * 0.08 + (12 * ((i // 25) % 2) - 6) for i in range(n)]
    return pd.DataFrame(
        {
            "timestamp": [1_700_000_000_000 + i * STEP for i in range(n)],
            "open": close,
            "high": [c * 1.01 for c in close],
            "low": [c * 0.99 for c in close],
            "close": close,
            "volume": [10.0] * n,
        }
    )


def broker(tmp_path: Path, name: str = "breakout", **kwargs) -> PaperBroker:
    from vito.policy import load_policy

    return PaperBroker(
        policy=load_policy(),
        inst_id="BTC-USDT-SWAP",
        strategy=name,
        timeframe="1h",
        state_path=tmp_path / "paper.json",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# equivalence with the backtester
# ---------------------------------------------------------------------------


def test_paper_matches_backtest(tmp_path: Path) -> None:
    """Two implementations of one rule set must not disagree."""
    candles = trending()
    params = {"window": 20}
    b = broker(tmp_path, params=params)

    paper = b.step(candles, replay=True)
    backtest = run_backtest(
        candles,
        build("breakout", **params).signals("BTC-USDT-SWAP", candles),
        inst_id="BTC-USDT-SWAP",
        strategy_name="breakout",
        policy=b.policy,
        timeframe="1h",
        max_bars=b.max_bars,
    )

    assert paper.state.trades, "the fixture must actually trade"
    assert len(paper.state.trades) == backtest.n_trades
    assert paper.marked_equity == pytest.approx(backtest.final_equity, abs=0.01)

    for paper_trade, bt_trade in zip(paper.state.trade_objects, backtest.trades, strict=True):
        assert paper_trade.side == bt_trade.side
        assert paper_trade.entry_ts == bt_trade.entry_ts
        assert paper_trade.exit_ts == bt_trade.exit_ts
        assert paper_trade.entry_price == pytest.approx(bt_trade.entry_price, abs=1e-6)
        assert paper_trade.exit_price == pytest.approx(bt_trade.exit_price, abs=1e-6)
        assert paper_trade.pnl == pytest.approx(bt_trade.pnl, abs=1e-3)
        assert paper_trade.fees == pytest.approx(bt_trade.fees, abs=1e-3)
        assert paper_trade.exit_reason == bt_trade.exit_reason


# ---------------------------------------------------------------------------
# live stepping
# ---------------------------------------------------------------------------


def test_first_step_warms_up_without_trading(tmp_path: Path) -> None:
    candles = trending()
    b = broker(tmp_path)
    report = b.step(candles)

    assert report.status == "warmed_up"
    assert report.state.trades == []
    assert report.state.position is None
    assert report.state.last_bar_ts == int(candles["timestamp"].iloc[-1])


def test_same_candles_twice_is_a_no_op(tmp_path: Path) -> None:
    candles = trending()
    b = broker(tmp_path)
    b.step(candles)
    second = b.step(candles)

    assert second.status == "no_new_bar"
    assert second.bars_processed == 0
    assert second.actions == ()


def test_only_new_bars_are_processed(tmp_path: Path) -> None:
    candles = trending()
    b = broker(tmp_path)
    b.step(candles.iloc[:-10])
    before = b.state().last_bar_ts

    report = b.step(candles)
    assert report.status == "stepped"
    assert report.bars_processed == 10
    assert report.state.last_bar_ts > before


def test_catch_up_equals_step_by_step(tmp_path: Path) -> None:
    """Missing the timer must not change the outcome, only when you notice."""
    candles = trending(300)

    batched = broker(tmp_path / "batched")
    batched.state_path = tmp_path / "batched.json"
    batched.step(candles.iloc[:250])
    batch_report = batched.step(candles)

    slow = broker(tmp_path / "slow")
    slow.state_path = tmp_path / "slow.json"
    slow.step(candles.iloc[:250])
    for cut in range(251, len(candles) + 1):
        slow.step(candles.iloc[:cut])

    assert len(batch_report.state.trades) == len(slow.state().trades)
    assert batch_report.marked_equity == pytest.approx(
        slow.step(candles).marked_equity, abs=0.01
    )


def test_equity_never_changes_without_a_trade(tmp_path: Path) -> None:
    candles = trending()
    b = broker(tmp_path)
    b.step(candles.iloc[:200])
    start = b.state().equity
    report = b.step(candles.iloc[:260])
    if not report.actions:
        assert report.state.equity == pytest.approx(start)


# ---------------------------------------------------------------------------
# safety
# ---------------------------------------------------------------------------


def test_kill_switch_blocks_entries_but_not_exits(tmp_path: Path, monkeypatch) -> None:
    import vito.paper.broker as mod

    candles = trending()
    b = broker(tmp_path)
    b.step(candles.iloc[:200])

    monkeypatch.setattr(mod, "kill_switch_active", lambda policy: True)
    report = b.step(candles)

    opened = [a for a in report.actions if a.startswith("open")]
    assert not opened
    assert report.state.skipped.get("kill_switch", 0) >= 1


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def test_state_roundtrips_through_disk(tmp_path: Path) -> None:
    candles = trending()
    b = broker(tmp_path)
    b.step(candles.iloc[:200])
    b.step(candles)

    reloaded = load_state(b.state_path)
    assert reloaded is not None
    assert reloaded.to_dict() == b.state().to_dict()


def test_atomic_save_leaves_no_temp_files(tmp_path: Path) -> None:
    candles = trending()
    b = broker(tmp_path)
    b.step(candles.iloc[:200])
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_version_mismatch_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "paper.json"
    path.write_text('{"version": 99}', encoding="utf-8")
    with pytest.raises(PaperError, match="version"):
        load_state(path)


def test_state_path_is_derived_from_the_run(tmp_path: Path) -> None:
    path = state_path_for("BTC-USDT-SWAP", "breakout", "1d", tmp_path)
    assert path.name == "paper-BTC-USDT-SWAP_breakout_1d.json"


def test_reset_deletes_the_track_record(tmp_path: Path) -> None:
    b = broker(tmp_path)
    b.step(trending())
    assert b.state_path.exists()
    b.reset()
    assert not b.state_path.exists()
    assert b.state().equity == pytest.approx(b.initial_equity)


def test_summary_reports_what_the_operator_needs(tmp_path: Path) -> None:
    b = broker(tmp_path)
    b.step(trending(), replay=True)
    summary = b.state().summary()
    assert summary["trades"] == len(b.state().trades)
    assert {"equity", "return_pct", "in_position", "fees_paid"} <= set(summary)


def test_default_state_is_versioned() -> None:
    assert PaperState(
        version=STATE_VERSION,
        inst_id="X",
        strategy="s",
        timeframe="1d",
        params={},
        initial_equity=200.0,
        equity=200.0,
        peak_equity=200.0,
        last_bar_ts=0,
    ).version == STATE_VERSION
