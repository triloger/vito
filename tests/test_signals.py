"""Unit tests for Vito's Tier-1 point-in-time signals and RiskOverlay."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from vito.backtest.panel import run_panel_momentum
from vito.signals import (
    FeatureStore,
    RiskOverlay,
    calendar_digest,
    classify_fear_greed,
    evaluate_market_clock,
    evaluate_overlay,
    events_within_window,
    fear_greed_at,
    funding_percentile_at,
    funding_swap_id,
    next_macro_event,
)


def _synth_panel(n: int = 80) -> dict[str, pd.DataFrame]:
    ts = np.arange(n, dtype=np.int64) * 86_400_000 + 1_700_000_000_000
    frames: dict[str, pd.DataFrame] = {}
    for idx, sym in enumerate(("BTC-USDT", "PAXG-USDT", "QQQ-USDT-SWAP")):
        drift = 0.005 * (idx + 1)
        wobble = 0.01 * np.sin(np.arange(n, dtype=float) * (idx + 1))
        close = 100.0 * np.exp(drift * np.arange(n) + wobble)
        open_ = np.roll(close, 1)
        open_[0] = close[0]
        high = np.maximum(open_, close) * 1.01
        low = np.minimum(open_, close) * 0.99
        frames[sym] = pd.DataFrame(
            {
                "timestamp": ts,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": np.full(n, 1000.0),
            }
        )
    return frames


def test_risk_overlay_clamps_to_unit_interval() -> None:
    ov = RiskOverlay(
        risk_scale=2.5,
        symbol_scales={"BTC-USDT": 1.8, "SOL-USDT": -0.5, "PAXG-USDT": 0.5},
        veto_symbols=frozenset({"ETH-USDT"}),
    )
    assert ov.risk_scale == 1.0
    assert ov.scale_for("BTC-USDT") == 1.0
    assert ov.scale_for("SOL-USDT") == 0.0
    assert ov.scale_for("PAXG-USDT") == 0.5
    assert ov.scale_for("ETH-USDT") == 0.0


def test_feature_store_hash_chain_and_point_in_time(tmp_path: Path) -> None:
    store = FeatureStore(tmp_path / "features.jsonl")
    h1 = store.append(key="fear_greed", obs_ts=1_000, value=45.0)
    h2 = store.append(key="fear_greed", obs_ts=2_000, value=82.0)
    assert h1 != h2
    assert store.verify() is True

    # Point-in-time query must not leak obs_ts=2000 at ts=1500
    assert store.as_of("fear_greed", 500) is None
    assert store.as_of("fear_greed", 1_500) == 45.0
    assert store.as_of("fear_greed", 2_500) == 82.0

    # Tamper with the file and verify chain fails
    lines = (tmp_path / "features.jsonl").read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[0])
    rec["feature"]["value"] = 99.0
    lines[0] = json.dumps(rec)
    (tmp_path / "features.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert store.verify() is False


def test_fear_greed_point_in_time_and_staleness() -> None:
    day = 86_400_000
    s = pd.Series([25.0, 55.0, 85.0], index=np.array([10 * day, 11 * day, 12 * day]))
    assert fear_greed_at(s, 9 * day) is None
    assert fear_greed_at(s, 10 * day) == 25.0
    assert fear_greed_at(s, 11 * day + 12 * 3_600_000) == 55.0
    assert fear_greed_at(s, 25 * day, max_staleness_days=7) is None
    assert classify_fear_greed(15) == "Extreme Fear"
    assert classify_fear_greed(85) == "High Greed"
    assert classify_fear_greed(92) == "Extreme Greed"


def test_macro_calendar_window_and_digest() -> None:
    assert len(calendar_digest()) == 16
    # 2026-10-28T18:00:00Z is an FOMC decision (2026-10-28T00:00:00Z = 1_793_145_600_000)
    ev = next_macro_event(1_793_145_600_000)
    assert ev is not None
    assert ev.kind == "FOMC"
    within = events_within_window(1_793_145_600_000, hours_ahead=24.0)
    assert len(within) == 1
    assert within[0].kind == "FOMC"


def test_funding_percentile_requires_min_obs_and_is_point_in_time() -> None:
    assert funding_swap_id("BTC-USDT") == "BTC-USDT-SWAP"
    assert funding_swap_id("QQQ-USDT-SWAP") == "QQQ-USDT-SWAP"

    step = 8 * 3_600_000
    ts = np.arange(120, dtype=np.int64) * step + 1_700_000_000_000
    vals = np.linspace(-0.0001, 0.0010, 120)
    s = pd.Series(vals, index=ts)

    # Fewer than 90 observations -> None
    assert funding_percentile_at(s, int(ts[50]), min_obs=90) is None
    # At the highest observation -> 100th percentile
    assert funding_percentile_at(s, int(ts[-1]), min_obs=90) == 100.0
    # At an earlier timestamp, future high values are invisible
    assert funding_percentile_at(s, int(ts[95]), min_obs=90) == 100.0


def test_evaluate_overlay_four_preregistered_rules() -> None:
    # Rule 4: Extreme fear (<= 10) never boosts risk_scale above 1.0
    ov_fear = evaluate_overlay(1_700_000_000_000, fear_greed=5.0, check_calendar=False)
    assert ov_fear.risk_scale == 1.0
    assert ov_fear.block_new_entries is False

    # Rule 2: Fear & Greed >= 80 -> 0.50x; >= 90 -> 0.25x
    ov_82 = evaluate_overlay(1_700_000_000_000, fear_greed=82.0, check_calendar=False)
    assert ov_82.risk_scale == 0.50
    ov_93 = evaluate_overlay(1_700_000_000_000, fear_greed=93.0, check_calendar=False)
    assert ov_93.risk_scale == 0.25

    # Rule 3: Symbol funding >= p90 -> 0.50x symbol scale
    ov_fund = evaluate_overlay(
        1_700_000_000_000,
        fear_greed=82.0,
        funding_pct={"BTC-USDT": 94.0, "PAXG-USDT": 40.0},
        check_calendar=False,
    )
    assert ov_fund.scale_for("BTC-USDT") == 0.25  # 0.50 global * 0.50 symbol
    assert ov_fund.scale_for("PAXG-USDT") == 0.50

    # Rule 1: FOMC within 24h -> block_new_entries = True
    ov_cal = evaluate_overlay(1_793_145_600_000, fear_greed=50.0, check_calendar=True)
    assert ov_cal.block_new_entries is True


def test_panel_overlay_none_and_identity_reproduce_baseline() -> None:
    frames = _synth_panel(80)
    base = run_panel_momentum(frames, initial_equity=93.16)
    ident = run_panel_momentum(
        frames,
        initial_equity=93.16,
        overlay_fn=lambda _: RiskOverlay(),
    )
    assert base.final_equity == ident.final_equity
    assert base.metrics["total_return_pct"] == ident.metrics["total_return_pct"]
    assert base.metrics["max_drawdown_pct"] == ident.metrics["max_drawdown_pct"]

    half = run_panel_momentum(
        frames,
        initial_equity=93.16,
        overlay_fn=lambda _: RiskOverlay(risk_scale=0.5),
    )
    assert half.final_equity < base.final_equity
    assert half.metrics["overlay_scaled_allocs"] > 0


def test_market_clock_us_sessions_and_funding_guard() -> None:
    # 2026-11-18 15:00 UTC == 10:00 EST (Wednesday, US Cash Open)
    ts_open = 1_795_014_000_000
    snap_open = evaluate_market_clock(
        ts_open, symbols=("BTC-USDT", "QQQ-USDT-SWAP"), funding_guard_minutes=15
    )
    assert snap_open.us_session == "US_CASH_OPEN"
    assert snap_open.us_cash_open is True
    assert snap_open.funding_guard_active is False
    assert len(snap_open.instruments) == 2
    assert snap_open.instruments[1].liquid_window is True

    # 2026-11-18 15:50 UTC (10 minutes before 16:00 UTC 8h funding) -> funding_guard_active True
    ts_guard = 1_795_017_000_000
    snap_guard = evaluate_market_clock(
        ts_guard, symbols=("QQQ-USDT-SWAP",), funding_guard_minutes=15
    )
    assert snap_guard.funding_guard_active is True

    # 2026-11-21 15:00 UTC (Saturday) -> WEEKEND_CLOSED
    ts_wknd = 1_795_273_200_000
    snap_wknd = evaluate_market_clock(
        ts_wknd, symbols=("QQQ-USDT-SWAP",), funding_guard_minutes=15
    )
    assert snap_wknd.us_session == "WEEKEND_CLOSED"
    assert snap_wknd.us_cash_open is False
    assert snap_wknd.instruments[0].liquid_window is False
