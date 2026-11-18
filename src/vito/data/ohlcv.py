"""Candle and funding history, with an on-disk cache.

Backtests must be reproducible: the same request returns the same bars. Anything
cached is stored under ``data/cache`` (gitignored) and reused unless ``refresh``
is set.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pandas as pd

CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "cache"

TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def timeframe_hours(timeframe: str) -> float:
    return TIMEFRAME_MS[timeframe] / 3_600_000


def _cache_path(kind: str, inst_id: str, timeframe: str, days: int, venue: str = "okx") -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    prefix = f"{kind}_binance" if venue == "binance" else kind
    return CACHE_DIR / f"{prefix}_{inst_id}_{timeframe}_{days}d.csv"


def _client_for(gateway: Any, market: dict[str, Any]) -> Any:
    if hasattr(gateway, "_client_for"):
        return gateway._client_for(market)  # noqa: SLF001
    return gateway.client


def fetch_recent(
    gateway: Any,
    inst_id: str,
    *,
    timeframe: str = "1d",
    limit: int = 500,
) -> pd.DataFrame:
    """The most recent bars, uncached.

    Paper trading has to see the newest close on every run, so caching here would
    be a bug: a stale file is indistinguishable from a quiet market.
    """
    market = gateway._market(inst_id)  # noqa: SLF001 - same package
    cli = _client_for(gateway, market)
    rows = cli.fetch_ohlcv(market["symbol"], timeframe=timeframe, limit=limit)
    return _frame(rows)


def closed_only(frame: pd.DataFrame, timeframe: str, *, now_ms: int | None = None) -> pd.DataFrame:
    """Drop the still-forming candle.

    Exchanges return the in-progress bar as the last row. Acting on it means
    acting on a price that has not closed, which is the live version of
    lookahead.
    """
    if frame.empty:
        return frame
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    closes_at = frame["timestamp"].astype("int64") + TIMEFRAME_MS[timeframe]
    return frame.loc[closes_at <= now].reset_index(drop=True)


def _frame(rows: list[list[float]]) -> pd.DataFrame:
    frame = pd.DataFrame(
        rows, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )
    return (
        frame.drop_duplicates(subset="timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )


def fetch_candles(
    gateway: Any,
    inst_id: str,
    *,
    timeframe: str = "4h",
    days: int = 730,
    refresh: bool = False,
) -> pd.DataFrame:
    """OHLCV for ``days`` back, paginated backwards from present and cached."""
    venue = str(getattr(gateway, "venue", "okx"))
    path = _cache_path("ohlcv", inst_id, timeframe, days, venue=venue)
    if path.exists() and not refresh:
        return pd.read_csv(path)

    market = gateway._market(inst_id)  # noqa: SLF001 - same package
    cli = _client_for(gateway, market)
    now = int(time.time() * 1000)
    since = now - days * 86_400_000
    rows: list[list[float]] = []

    if venue == "binance":
        end_cursor: int | None = now
        max_pages = max(10, int(days * 86_400_000 / TIMEFRAME_MS[timeframe] / 900) + 5)
        for _ in range(max_pages):
            params = {"endTime": end_cursor} if end_cursor is not None else {}
            batch = cli.fetch_ohlcv(
                market["symbol"], timeframe=timeframe, limit=1000, params=params
            )
            if not batch:
                break
            rows.extend(batch)
            oldest = int(batch[0][0])
            if oldest <= since or len(batch) < 1000:
                break
            if end_cursor is not None and oldest >= end_cursor:
                break
            end_cursor = oldest - 1
    else:
        after_cursor: int | None = None
        use_history = False
        max_pages = max(20, int(days * 86_400_000 / TIMEFRAME_MS[timeframe] / 250) + 10)
        for _ in range(max_pages):
            params_okx: dict[str, str] = {}
            if after_cursor is not None:
                params_okx["after"] = str(after_cursor)
            if use_history:
                params_okx["type"] = "HistoryCandles"
            batch = cli.fetch_ohlcv(
                market["symbol"], timeframe=timeframe, limit=300, params=params_okx
            )
            if not batch and not use_history and after_cursor is not None:
                use_history = True
                params_okx["type"] = "HistoryCandles"
                batch = cli.fetch_ohlcv(
                    market["symbol"], timeframe=timeframe, limit=300, params=params_okx
                )
            if not batch:
                break
            rows.extend(batch)
            oldest = int(batch[0][0])
            if oldest <= since:
                break
            if len(batch) < 300 and not use_history:
                use_history = True
            elif len(batch) < 300 and use_history:
                break
            if after_cursor is not None and oldest >= after_cursor:
                break
            after_cursor = oldest

    frame = _frame(rows)
    if not frame.empty:
        frame = frame.loc[frame["timestamp"] >= since].reset_index(drop=True)
    frame.to_csv(path, index=False)
    return frame


def fetch_funding(
    gateway: Any,
    inst_id: str,
    *,
    days: int = 730,
    refresh: bool = False,
) -> pd.Series:
    """Historical funding rates per interval, indexed by timestamp (ms)."""
    venue = str(getattr(gateway, "venue", "okx"))
    path = _cache_path("funding", inst_id, "8h", days, venue=venue)
    if path.exists() and not refresh:
        frame = pd.read_csv(path)
        return pd.Series(frame["rate"].to_numpy(), index=frame["timestamp"].to_numpy())

    market = gateway._market(inst_id)  # noqa: SLF001
    if market.get("type") != "swap":
        return pd.Series(dtype=float)
    cli = _client_for(gateway, market)
    now = int(time.time() * 1000)
    since = now - days * 86_400_000
    rows: list[tuple[int, float]] = []

    if venue == "binance":
        end_cursor: int | None = now
        max_pages = max(10, int(days * 3 / 450) + 5)
        for _ in range(max_pages):
            params = {"endTime": end_cursor} if end_cursor is not None else {}
            try:
                batch = cli.fetch_funding_rate_history(
                    market["symbol"], limit=1000, params=params
                )
            except Exception:  # noqa: BLE001 - funding is best-effort
                break
            if not batch:
                break
            for item in batch:
                rows.append((int(item["timestamp"]), float(item["fundingRate"])))
            oldest = int(batch[0]["timestamp"])
            if oldest <= since or len(batch) < 500:
                break
            if end_cursor is not None and oldest >= end_cursor:
                break
            end_cursor = oldest - 1
    else:
        after_cursor: int | None = None
        max_pages = max(15, int(days * 3 / 90) + 5)
        for _ in range(max_pages):
            params_okx = {"after": str(after_cursor)} if after_cursor is not None else {}
            try:
                batch = cli.fetch_funding_rate_history(
                    market["symbol"], limit=100, params=params_okx
                )
            except Exception:  # noqa: BLE001 - funding is best-effort
                break
            if not batch:
                break
            for item in batch:
                rows.append((int(item["timestamp"]), float(item["fundingRate"])))
            oldest = int(batch[0]["timestamp"])
            if oldest <= since or len(batch) < 100:
                break
            if after_cursor is not None and oldest >= after_cursor:
                break
            after_cursor = oldest

    if not rows:
        return pd.Series(dtype=float)

    frame = pd.DataFrame(rows, columns=["timestamp", "rate"]).drop_duplicates("timestamp")
    frame = frame.loc[frame["timestamp"] >= since].sort_values("timestamp").reset_index(drop=True)
    frame.to_csv(path, index=False)
    return pd.Series(frame["rate"].to_numpy(), index=frame["timestamp"].to_numpy())
