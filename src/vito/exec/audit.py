"""Append-only, hash-chained audit log.

Every decision — including every refusal — is appended as a JSON line. Each
line embeds the hash of the previous line, so tampering with history is
detectable by replaying the chain.

Format::

    {"seq": 0, "ts": "...", "prev": "genesis", "event": {...}, "hash": "..."}
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
class AuditLog:
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

    def append(self, event: dict[str, Any]) -> str:
        """Append one event; returns its hash."""
        seq = self._next_seq()
        record = {
            "seq": seq,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "prev": self._prev,
            "event": event,
        }
        digest = hashlib.sha256(
            (record["prev"] + json.dumps(event, sort_keys=True, default=str)).encode()
        ).hexdigest()
        record["hash"] = digest

        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

        self._prev = digest
        return digest

    def _next_seq(self) -> int:
        if not self.path.exists():
            return 0
        with self.path.open("r", encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())

    def verify(self) -> bool:
        """Replay the chain. Returns False if any link is broken."""
        prev = GENESIS
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                expected = hashlib.sha256(
                    (
                        prev + json.dumps(record["event"], sort_keys=True, default=str)
                    ).encode()
                ).hexdigest()
                if record.get("hash") != expected or record.get("prev") != prev:
                    return False
                prev = expected
        return True
