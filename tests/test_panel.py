from __future__ import annotations

import pandas as pd
import pytest

from vito.backtest.panel import run_panel_momentum

STEP = 86_400_000


def _frame(closes: list[float], start_ts: int = 1_700_000_000_000) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": [start_ts + i * STEP for i in range(len(closes))],
            "open": closes,
            "high": [c * 1.005 for c in closes],
            "low": [c * 0.995 for c in closes],
            "close": closes,
            "volume": [100.0] * len(closes),
        }
    )


def test_panel_selects_uptrending_winner_and_avoids_downtrend() -> None:
    n = 80
    up = [100.0 * (1.01 ** (i + (i % 3) * 0.1)) for i in range(n)]
    down = [100.0 * (0.99 ** (i + (i % 3) * 0.1)) for i in range(n)]
    res = run_panel_momentum(
        {"WINNER": _frame(up), "LOSER": _frame(down)},
        lookback=15,
        rebalance_every=5,
        max_positions=1,
        max_gross_exposure_mult=0.75,
        fee_bps=0.0,
        slippage_bps=0.0,
    )
    assert res.final_equity > res.initial_equity
    assert res.asset_stats["WINNER"]["bars_held"] > 0
    assert res.asset_stats["LOSER"]["bars_held"] == 0


def test_panel_holds_cash_when_all_assets_are_falling() -> None:
    n = 60
    down1 = [100.0 * (0.99 ** (i + (i % 2) * 0.2)) for i in range(n)]
    down2 = [200.0 * (0.985 ** (i + (i % 3) * 0.2)) for i in range(n)]
    res = run_panel_momentum(
        {"A": _frame(down1), "B": _frame(down2)},
        lookback=15,
        rebalance_every=5,
        max_positions=2,
    )
    assert res.final_equity == pytest.approx(res.initial_equity)
    assert res.metrics["exposure_pct"] == 0.0
