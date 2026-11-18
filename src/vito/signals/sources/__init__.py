"""External point-in-time signal sources (Fear & Greed, macro calendar, funding)."""

from vito.signals.sources.calendar import (
    MACRO_EVENTS,
    MacroEvent,
    calendar_digest,
    events_within_window,
    next_macro_event,
)
from vito.signals.sources.feargreed import (
    classify_fear_greed,
    fear_greed_at,
    fetch_fear_greed,
)
from vito.signals.sources.funding import (
    fetch_funding_panel,
    funding_percentile_at,
    funding_swap_id,
    latest_funding_rate_at,
)

__all__ = [
    "MACRO_EVENTS",
    "MacroEvent",
    "calendar_digest",
    "classify_fear_greed",
    "events_within_window",
    "fear_greed_at",
    "fetch_fear_greed",
    "fetch_funding_panel",
    "funding_percentile_at",
    "funding_swap_id",
    "latest_funding_rate_at",
    "next_macro_event",
]
