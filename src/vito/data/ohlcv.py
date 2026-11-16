"""Candle and funding history, with an on-disk cache.

Backtests must be reproducible: the same request returns the same bars. Anything
cached is stored under ``data/cache`` (gitignored) and reused unless ``refresh``
is set.
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from vito.exchange.okx import OkxGateway

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


def _cache_path(kind: str, inst_id: str, timeframe: str, days: int) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{kind}_{inst_id}_{timeframe}_{days}d.csv"


def fetch_recent(
    gateway: OkxGateway,
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
    rows = gateway.client.fetch_ohlcv(market["symbol"], timeframe=timeframe, limit=limit)
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
    gateway: OkxGateway,
    inst_id: str,
    *,
    timeframe: str = "4h",
    days: int = 730,
    refresh: bool = False,
) -> pd.DataFrame:
    """OHLCV for ``days`` back, paginated backwards from present and cached.

    Paging backwards via ``after`` (oldest timestamp seen) ensures instruments
    listed more recently than ``days`` ago still return their full history
    rather than an empty initial page at ``since``.
    """
    path = _cache_path("ohlcv", inst_id, timeframe, days)
    if path.exists() and not refresh:
        return pd.read_csv(path)

    market = gateway._market(inst_id)  # noqa: SLF001 - same package
    now = int(time.time() * 1000)
    since = now - days * 86_400_000

    rows: list[list[float]] = []
    after_cursor: int | None = None
    max_pages = max(20, int(days * 86_400_000 / TIMEFRAME_MS[timeframe] / 90) + 5)

    for _ in range(max_pages):
        params = {"after": str(after_cursor)} if after_cursor is not None else {}
        batch = gateway.client.fetch_ohlcv(
            market["symbol"], timeframe=timeframe, limit=100, params=params
        )
        if not batch:
            break
        rows.extend(batch)
        oldest = int(batch[0][0])
        if oldest <= since or len(batch) < 100:
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
    gateway: OkxGateway,
    inst_id: str,
    *,
    days: int = 730,
    refresh: bool = False,
) -> pd.Series:
    """Historical funding rates per interval, indexed by timestamp (ms)."""
    path = _cache_path("funding", inst_id, "8h", days)
    if path.exists() and not refresh:
        frame = pd.read_csv(path)
        return pd.Series(frame["rate"].to_numpy(), index=frame["timestamp"].to_numpy())

    market = gateway._market(inst_id)  # noqa: SLF001
    since = int(time.time() * 1000) - days * 86_400_000
    rows: list[tuple[int, float]] = []
    after_cursor: int | None = None
    max_pages = max(15, int(days * 3 / 90) + 5)

    for _ in range(max_pages):
        params = {"after": str(after_cursor)} if after_cursor is not None else {}
        try:
            batch = gateway.client.fetch_funding_rate_history(
                market["symbol"], limit=100, params=params
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
