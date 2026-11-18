"""Point-in-time external signals and risk-reducing overlay for Vito."""

from vito.signals.overlay import RiskOverlay, build_panel_overlay_fn, evaluate_overlay
from vito.signals.sources import (
    MACRO_EVENTS,
    MacroEvent,
    calendar_digest,
    classify_fear_greed,
    events_within_window,
    fear_greed_at,
    fetch_fear_greed,
    fetch_funding_panel,
    funding_percentile_at,
    funding_swap_id,
    latest_funding_rate_at,
    next_macro_event,
)
from vito.signals.store import FeatureStore

__all__ = [
    "MACRO_EVENTS",
    "FeatureStore",
    "MacroEvent",
    "RiskOverlay",
    "build_panel_overlay_fn",
    "calendar_digest",
    "classify_fear_greed",
    "evaluate_overlay",
    "events_within_window",
    "fear_greed_at",
    "fetch_fear_greed",
    "fetch_funding_panel",
    "funding_percentile_at",
    "funding_swap_id",
    "latest_funding_rate_at",
    "next_macro_event",
]
