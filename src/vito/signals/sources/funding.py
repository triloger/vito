"""Perpetual funding crowding telemetry.

For each basket instrument (including Spot legs such as ``BTC-USDT`` whose
positioning crowding is observable on ``BTC-USDT-SWAP``), computes the point-in-time
percentile of the latest 8h funding rate relative to its trailing 1-year window.

Point-in-time rule:
- Only funding observations with ``timestamp <= ts_ms`` are included.
- Requires at least ``min_obs`` (default 90, ~30 days of 8h prints) in the
  trailing window; otherwise returns ``None`` (no data -> no penalty).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from vito.data.ohlcv import fetch_funding


def funding_swap_id(inst_id: str) -> str:
    """Map any Spot symbol (``BTC-USDT``) to its perp swap (``BTC-USDT-SWAP``)."""
    return inst_id if inst_id.endswith("-SWAP") else f"{inst_id}-SWAP"


def fetch_funding_panel(
    gateway: Any,
    symbols: list[str] | tuple[str, ...],
    *,
    days: int = 1460,
    refresh: bool = False,
) -> dict[str, pd.Series]:
    """Fetch historical 8h funding series keyed by the basket symbol name."""
    out: dict[str, pd.Series] = {}
    for sym in symbols:
        swap_sym = funding_swap_id(sym)
        try:
            s = fetch_funding(gateway, swap_sym, days=days, refresh=refresh)
        except Exception:  # noqa: BLE001 - best-effort if swap does not exist
            s = pd.Series(dtype=float)
        out[sym] = s
    return out


def funding_percentile_at(
    series: pd.Series,
    ts_ms: int,
    *,
    window_days: int = 365,
    min_obs: int = 90,
) -> float | None:
    """Return trailing percentile (0–100) of the latest funding rate at ``ts_ms``."""
    if series.empty:
        return None
    idx = series.index.to_numpy(dtype=np.int64)
    vals = series.to_numpy(dtype=float)

    end_pos = int(np.searchsorted(idx, int(ts_ms), side="right"))
    if end_pos <= 0:
        return None
    start_ts = int(ts_ms) - window_days * 86_400_000
    start_pos = int(np.searchsorted(idx, start_ts, side="left"))

    window = vals[start_pos:end_pos]
    window = window[np.isfinite(window)]
    if len(window) < min_obs:
        return None

    latest = float(window[-1])
    # Use strict `<` against prior window observations so Binance's pinned
    # neutral interest-rate clamp (0.0100% / 0.0050% per 8h) does not score as
    # p90+ on normal uncrowded days.
    prior = window[:-1]
    pct = float(np.mean(prior < latest) * 100.0) if len(prior) else 50.0
    return round(pct, 2)


def latest_funding_rate_at(series: pd.Series, ts_ms: int) -> float | None:
    """Return the most recent 8h funding rate (decimal) at or before ``ts_ms``."""
    if series.empty:
        return None
    idx = series.index.to_numpy(dtype=np.int64)
    pos = int(np.searchsorted(idx, int(ts_ms), side="right")) - 1
    if pos < 0:
        return None
    val = float(series.iloc[pos])
    return val if np.isfinite(val) else None
