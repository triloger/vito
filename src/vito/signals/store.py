"""Append-only, hash-chained point-in-time feature store.

Every external signal observation (Fear & Greed, funding crowding percentile,
macro event proximity) recorded by Vito is stamped with:
- ``obs_ts``: point-in-time timestamp (ms UTC) when the value became known.
- ``key``: feature identifier (e.g. ``"fear_greed"``, ``"funding_pct:BTC-USDT"``).
- ``value``: numeric or structured payload.
- ``prev`` & ``hash``: SHA-256 chain identical in discipline to ``AuditLog``.

Point-in-time queries (`as_of(key, ts_ms)`) strictly filter ``obs_ts <= ts_ms``
so backtests and replay runs cannot leak future data into earlier bars.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


@dataclass
class FeatureStore:
    path: Path
    _prev: str = field(default=GENESIS, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._prev = self._tail_hash()

    def _tail_hash(self) -> str:
        last = GENESIS
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last = json.loads(line).get("hash", last)
        return last

    def _next_seq(self) -> int:
        if not self.path.exists():
            return 0
        with self.path.open("r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())

    def append(
        self, *, key: str, obs_ts: int, value: Any, meta: dict[str, Any] | None = None
    ) -> str:
        """Append a point-in-time feature observation and return its chain hash."""
        payload = {
            "key": str(key),
            "obs_ts": int(obs_ts),
            "value": value,
            "meta": meta or {},
        }
        seq = self._next_seq()
        record = {
            "seq": seq,
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "prev": self._prev,
            "feature": payload,
        }
        digest = hashlib.sha256(
            (record["prev"] + json.dumps(payload, sort_keys=True, default=str)).encode()
        ).hexdigest()
        record["hash"] = digest

        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

        self._prev = digest
        return digest

    def verify(self) -> bool:
        """Verify the SHA-256 hash chain across all stored observations."""
        if not self.path.exists():
            return True
        prev = GENESIS
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                expected = hashlib.sha256(
                    (
                        prev + json.dumps(record["feature"], sort_keys=True, default=str)
                    ).encode()
                ).hexdigest()
                if record.get("hash") != expected or record.get("prev") != prev:
                    return False
                prev = expected
        return True

    def as_of(self, key: str, ts_ms: int) -> Any | None:
        """Return the latest feature value for ``key`` with ``obs_ts <= ts_ms``."""
        if not self.path.exists():
            return None
        best_ts: int | None = None
        best_val: Any | None = None
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                feat = json.loads(line).get("feature", {})
                if feat.get("key") != key:
                    continue
                obs_ts = int(feat.get("obs_ts", 0))
                if obs_ts <= ts_ms and (best_ts is None or obs_ts >= best_ts):
                    best_ts = obs_ts
                    best_val = feat.get("value")
        return best_val
