"""Crypto Fear & Greed Index source (alternative.me).

Provides daily sentiment readings (0 = Extreme Fear, 100 = Extreme Greed) back
to February 2018 with no API key required. Results are cached locally under
``~/.vito/cache/feargreed.csv``.

Point-in-time rule:
- ``fear_greed_at(series, ts_ms)`` uses ``searchsorted(..., side="right") - 1``
  so only observations with ``timestamp <= ts_ms`` are visible.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

FNG_URL = "https://api.alternative.me/fng/?limit=0&format=json"
DEFAULT_CACHE = Path("~/.vito/cache/feargreed.csv").expanduser()


def classify_fear_greed(value: float) -> str:
    """Human-readable regime label for a 0–100 Fear & Greed score."""
    if value <= 20:
        return "Extreme Fear"
    if value <= 40:
        return "Fear"
    if value < 60:
        return "Neutral"
    if value < 80:
        return "Greed"
    if value < 90:
        return "High Greed"
    return "Extreme Greed"


def fetch_fear_greed(
    *,
    cache_path: Path = DEFAULT_CACHE,
    refresh: bool = False,
    max_age_sec: int = 21_600,
    timeout: float = 15.0,
) -> pd.Series:
    """Return daily Fear & Greed scores (0–100) indexed by UTC timestamp in ms."""
    cache_path = Path(cache_path).expanduser()
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if cache_path.exists() and not refresh:
        age = time.time() - cache_path.stat().st_mtime
        if age <= max_age_sec:
            return _read_cache(cache_path)

    try:
        req = urllib.request.Request(
            FNG_URL,
            headers={"User-Agent": "vito-treasury/0.1"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
        rows: list[tuple[int, float]] = []
        for item in payload.get("data", []):
            ts_sec = int(item["timestamp"])
            val = float(item["value"])
            rows.append((ts_sec * 1000, val))
        if rows:
            df = (
                pd.DataFrame(rows, columns=["timestamp", "value"])
                .drop_duplicates("timestamp")
                .sort_values("timestamp")
                .reset_index(drop=True)
            )
            df.to_csv(cache_path, index=False)
            return pd.Series(
                df["value"].to_numpy(dtype=float),
                index=df["timestamp"].to_numpy(dtype=np.int64),
            )
    except Exception:  # noqa: BLE001 - fall back to cached file if offline
        if cache_path.exists():
            return _read_cache(cache_path)

    return pd.Series(dtype=float)


def _read_cache(cache_path: Path) -> pd.Series:
    df = pd.read_csv(cache_path)
    if df.empty:
        return pd.Series(dtype=float)
    df = df.sort_values("timestamp").reset_index(drop=True)
    return pd.Series(
        df["value"].to_numpy(dtype=float),
        index=df["timestamp"].to_numpy(dtype=np.int64),
    )


def fear_greed_at(series: pd.Series, ts_ms: int, *, max_staleness_days: int = 7) -> float | None:
    """Return the latest Fear & Greed score known at or before ``ts_ms``."""
    if series.empty:
        return None
    idx = series.index.to_numpy(dtype=np.int64)
    pos = int(np.searchsorted(idx, int(ts_ms), side="right")) - 1
    if pos < 0:
        return None
    obs_ts = int(idx[pos])
    if (int(ts_ms) - obs_ts) > max_staleness_days * 86_400_000:
        return None
    val = float(series.iloc[pos])
    return val if np.isfinite(val) else None
