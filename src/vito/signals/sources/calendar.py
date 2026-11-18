"""Versioned US macro event calendar (FOMC rate decisions & BLS CPI releases).

Scheduled macro releases are known months in advance from the Federal Reserve
and Bureau of Labor Statistics calendars. Unlike price or headline data, checking
whether a scheduled FOMC or CPI release falls within the next 24 hours is strictly
point-in-time valid because the calendar itself is public beforehand.

All timestamps are stored in UTC milliseconds:
- FOMC rate decisions: 18:00 UTC (EDT) / 19:00 UTC (EST) on decision day.
- US CPI releases:     12:30 UTC (EDT) / 13:30 UTC (EST) on release day.
"""

from __future__ import annotations

import calendar as _cal
import hashlib
import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MacroEvent:
    ts_ms: int
    kind: str  # "FOMC" | "CPI"
    iso_utc: str
    label: str


def _utc_ms(iso_utc: str) -> int:
    tm = time.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ")
    return int(_cal.timegm(tm) * 1000)


# Canonical schedule covering the 4-year backtest window (2022-11 → 2026-12)
# plus 2027 forward schedule.
_RAW_SCHEDULE: tuple[tuple[str, str, str], ...] = (
    # --- 2022 Q4 ---
    ("2022-11-02T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2022-11-10T13:30:00Z", "CPI", "US CPI Release"),
    ("2022-12-13T13:30:00Z", "CPI", "US CPI Release"),
    ("2022-12-14T19:00:00Z", "FOMC", "FOMC Rate Decision"),
    # --- 2023 ---
    ("2023-01-12T13:30:00Z", "CPI", "US CPI Release"),
    ("2023-02-01T19:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2023-02-14T13:30:00Z", "CPI", "US CPI Release"),
    ("2023-03-14T12:30:00Z", "CPI", "US CPI Release"),
    ("2023-03-22T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2023-04-12T12:30:00Z", "CPI", "US CPI Release"),
    ("2023-05-03T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2023-05-10T12:30:00Z", "CPI", "US CPI Release"),
    ("2023-06-13T12:30:00Z", "CPI", "US CPI Release"),
    ("2023-06-14T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2023-07-12T12:30:00Z", "CPI", "US CPI Release"),
    ("2023-07-26T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2023-08-10T12:30:00Z", "CPI", "US CPI Release"),
    ("2023-09-13T12:30:00Z", "CPI", "US CPI Release"),
    ("2023-09-20T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2023-10-12T12:30:00Z", "CPI", "US CPI Release"),
    ("2023-11-01T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2023-11-14T13:30:00Z", "CPI", "US CPI Release"),
    ("2023-12-12T13:30:00Z", "CPI", "US CPI Release"),
    ("2023-12-13T19:00:00Z", "FOMC", "FOMC Rate Decision"),
    # --- 2024 ---
    ("2024-01-11T13:30:00Z", "CPI", "US CPI Release"),
    ("2024-01-31T19:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2024-02-13T13:30:00Z", "CPI", "US CPI Release"),
    ("2024-03-12T12:30:00Z", "CPI", "US CPI Release"),
    ("2024-03-20T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2024-04-10T12:30:00Z", "CPI", "US CPI Release"),
    ("2024-05-01T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2024-05-15T12:30:00Z", "CPI", "US CPI Release"),
    ("2024-06-12T12:30:00Z", "CPI", "US CPI Release"),
    ("2024-06-12T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2024-07-11T12:30:00Z", "CPI", "US CPI Release"),
    ("2024-07-31T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2024-08-14T12:30:00Z", "CPI", "US CPI Release"),
    ("2024-09-11T12:30:00Z", "CPI", "US CPI Release"),
    ("2024-09-18T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2024-10-10T12:30:00Z", "CPI", "US CPI Release"),
    ("2024-11-07T19:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2024-11-13T13:30:00Z", "CPI", "US CPI Release"),
    ("2024-12-11T13:30:00Z", "CPI", "US CPI Release"),
    ("2024-12-18T19:00:00Z", "FOMC", "FOMC Rate Decision"),
    # --- 2025 ---
    ("2025-01-15T13:30:00Z", "CPI", "US CPI Release"),
    ("2025-01-29T19:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2025-02-12T13:30:00Z", "CPI", "US CPI Release"),
    ("2025-03-12T12:30:00Z", "CPI", "US CPI Release"),
    ("2025-03-19T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2025-04-10T12:30:00Z", "CPI", "US CPI Release"),
    ("2025-05-07T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2025-05-13T12:30:00Z", "CPI", "US CPI Release"),
    ("2025-06-11T12:30:00Z", "CPI", "US CPI Release"),
    ("2025-06-18T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2025-07-15T12:30:00Z", "CPI", "US CPI Release"),
    ("2025-07-30T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2025-08-12T12:30:00Z", "CPI", "US CPI Release"),
    ("2025-09-11T12:30:00Z", "CPI", "US CPI Release"),
    ("2025-09-17T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2025-10-15T12:30:00Z", "CPI", "US CPI Release"),
    ("2025-10-29T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2025-11-13T13:30:00Z", "CPI", "US CPI Release"),
    ("2025-12-10T13:30:00Z", "CPI", "US CPI Release"),
    ("2025-12-10T19:00:00Z", "FOMC", "FOMC Rate Decision"),
    # --- 2026 ---
    ("2026-01-13T13:30:00Z", "CPI", "US CPI Release"),
    ("2026-01-28T19:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2026-02-11T13:30:00Z", "CPI", "US CPI Release"),
    ("2026-03-11T12:30:00Z", "CPI", "US CPI Release"),
    ("2026-03-18T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2026-04-14T12:30:00Z", "CPI", "US CPI Release"),
    ("2026-04-29T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2026-05-12T12:30:00Z", "CPI", "US CPI Release"),
    ("2026-06-10T12:30:00Z", "CPI", "US CPI Release"),
    ("2026-06-17T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2026-07-14T12:30:00Z", "CPI", "US CPI Release"),
    ("2026-07-29T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2026-08-12T12:30:00Z", "CPI", "US CPI Release"),
    ("2026-09-11T12:30:00Z", "CPI", "US CPI Release"),
    ("2026-09-16T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2026-10-14T12:30:00Z", "CPI", "US CPI Release"),
    ("2026-10-28T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2026-11-12T13:30:00Z", "CPI", "US CPI Release"),
    ("2026-12-09T19:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2026-12-10T13:30:00Z", "CPI", "US CPI Release"),
    # --- 2027 ---
    ("2027-01-13T13:30:00Z", "CPI", "US CPI Release"),
    ("2027-01-27T19:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2027-02-10T13:30:00Z", "CPI", "US CPI Release"),
    ("2027-03-10T13:30:00Z", "CPI", "US CPI Release"),
    ("2027-03-17T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2027-04-13T12:30:00Z", "CPI", "US CPI Release"),
    ("2027-04-28T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2027-05-12T12:30:00Z", "CPI", "US CPI Release"),
    ("2027-06-09T18:00:00Z", "FOMC", "FOMC Rate Decision"),
    ("2027-06-15T12:30:00Z", "CPI", "US CPI Release"),
)

MACRO_EVENTS: tuple[MacroEvent, ...] = tuple(
    sorted(
        (
            MacroEvent(ts_ms=_utc_ms(iso), kind=kind, iso_utc=iso, label=label)
            for iso, kind, label in _RAW_SCHEDULE
        ),
        key=lambda e: e.ts_ms,
    )
)


def calendar_digest() -> str:
    """Return 16-char SHA-256 hex digest of the canonical macro event table."""
    blob = "\n".join(f"{e.iso_utc}|{e.kind}|{e.label}" for e in MACRO_EVENTS)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def events_within_window(ts_ms: int, *, hours_ahead: float = 24.0) -> tuple[MacroEvent, ...]:
    """Return all macro events occurring in ``[ts_ms, ts_ms + hours_ahead]``."""
    horizon = int(ts_ms + hours_ahead * 3_600_000)
    return tuple(ev for ev in MACRO_EVENTS if int(ts_ms) <= ev.ts_ms <= horizon)


def next_macro_event(ts_ms: int) -> MacroEvent | None:
    """Return the next scheduled macro event at or after ``ts_ms``."""
    for ev in MACRO_EVENTS:
        if ev.ts_ms >= int(ts_ms):
            return ev
    return None
