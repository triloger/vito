from __future__ import annotations

import pandas as pd
import pytest

from vito.backtest.panel import (
    compute_index_gear,
    evaluate_index_gear_at,
    rank_panel_at,
    run_panel_momentum,
)

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


def test_panel_intrabar_stop_caps_crash_between_rebalances() -> None:
    closes = [100.0 * (1.01 ** (i + (i % 3) * 0.1)) for i in range(30)]
    # From bar 23 onwards, asset crashes -40% intrabar and stays down
    df = _frame(closes)
    df.loc[23, "low"] = float(df.loc[22, "close"]) * 0.60
    df.loc[23, "close"] = float(df.loc[22, "close"]) * 0.60
    for k in range(24, 30):
        df.loc[k, ["open", "high", "low", "close"]] = float(df.loc[22, "close"]) * 0.60

    stopped = run_panel_momentum(
        {"SPIKER": df},
        lookback=15,
        rebalance_every=5,
        max_positions=1,
        use_stops=True,
    )
    unstopped = run_panel_momentum(
        {"SPIKER": df},
        lookback=15,
        rebalance_every=5,
        max_positions=1,
        use_stops=False,
    )
    assert stopped.metrics["stops_hit"] >= 1
    assert stopped.final_equity > unstopped.final_equity


def test_paper_panel_broker_warms_up_and_steps(tmp_path) -> None:
    from vito.paper import PaperPanelBroker
    from vito.policy import load_policy

    n = 40
    up = _frame([100.0 * (1.01 ** (i + (i % 3) * 0.1)) for i in range(n)])
    down = _frame([100.0 * (0.99 ** (i + (i % 3) * 0.1)) for i in range(n)])
    broker = PaperPanelBroker(
        policy=load_policy(),
        symbols=["UP", "DOWN"],
        lookback=15,
        rebalance_every=5,
        state_path=tmp_path / "paper-panel.json",
    )
    w = broker.step({"UP": up.iloc[:30], "DOWN": down.iloc[:30]})
    assert w.status == "warmed_up"
    assert w.state.params["pending_targets"][0]["sym"] == "UP"

    s = broker.step({"UP": up, "DOWN": down})
    assert s.status == "stepped"
    assert s.bars_processed == 10
    assert s.state.equity > s.state.initial_equity


def test_panel_max_per_group_prevents_same_family_concentration() -> None:
    n = 50
    # Two precious metals (PAXG, XAG) outperforming one equity index (QQQ)
    paxg = _frame([100.0 * (1.015 ** (i + (i % 3) * 0.1)) for i in range(n)])
    xag = _frame([100.0 * (1.012 ** (i + (i % 3) * 0.1)) for i in range(n)])
    qqq = _frame([100.0 * (1.008 ** (i + (i % 3) * 0.1)) for i in range(n)])
    frames = {"PAXG-USDT": paxg, "XAG-USDT-SWAP": xag, "QQQ-USDT-SWAP": qqq}

    uncapped = rank_panel_at(frames, lookback=15, max_positions=2, max_per_group=None)
    assert [r[0] for r in uncapped] == ["PAXG-USDT", "XAG-USDT-SWAP"]

    capped = rank_panel_at(frames, lookback=15, max_positions=2, max_per_group=1)
    assert [r[0] for r in capped] == ["PAXG-USDT", "QQQ-USDT-SWAP"]


def test_index_gear_rewards_low_vol_and_drops_when_vol_expands() -> None:
    # 1. Non-index asset never gears
    g_btc, _ = compute_index_gear(
        "BTC-USDT", score_20d=8.0, ann_vol_20d=0.12, above_long_sma=True, max_gear=3.0
    )
    assert g_btc == 1.0

    # 2. Index asset with strong score (>=5.5), low vol (<=17%), above 200d SMA -> 3.0x
    g_qqq, reason = compute_index_gear(
        "QQQ-USDT-SWAP", score_20d=6.8, ann_vol_20d=0.14, above_long_sma=True, max_gear=3.0
    )
    assert g_qqq == 3.0
    assert "3.0x" in reason

    # 3. Vol expands above 17% -> immediately drops back to 1.0x
    g_hi_vol, reason_vol = compute_index_gear(
        "QQQ-USDT-SWAP", score_20d=6.8, ann_vol_20d=0.22, above_long_sma=True, max_gear=3.0
    )
    assert g_hi_vol == 1.0
    assert "vol_" in reason_vol

    # 4. Below 200d SMA -> 1.0x
    g_below, _ = compute_index_gear(
        "QQQ-USDT-SWAP", score_20d=6.8, ann_vol_20d=0.14, above_long_sma=False, max_gear=3.0
    )
    assert g_below == 1.0

    # 5. End-to-end in evaluate_index_gear_at and run_panel_momentum
    n = 60
    calm_qqq = _frame([100.0 * (1.004 ** (i + (i % 2) * 0.05)) for i in range(n)])
    states = evaluate_index_gear_at({"QQQ-USDT-SWAP": calm_qqq}, lookback=20, max_gear=3.0)
    assert len(states) == 1
    assert states[0].inst_id == "QQQ-USDT-SWAP"
    assert states[0].gear_mult == 3.0

    res_1x = run_panel_momentum(
        {"QQQ-USDT-SWAP": calm_qqq},
        lookback=20,
        max_positions=1,
        index_gear_mult=1.0,
        fee_bps=0.0,
        slippage_bps=0.0,
    )
    res_3x = run_panel_momentum(
        {"QQQ-USDT-SWAP": calm_qqq},
        lookback=20,
        max_positions=1,
        index_gear_mult=3.0,
        fee_bps=0.0,
        slippage_bps=0.0,
    )
    assert res_3x.final_equity > res_1x.final_equity
    assert res_3x.metrics["index_geared_allocs"] > 0
