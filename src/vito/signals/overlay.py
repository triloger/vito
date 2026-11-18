"""Deterministic, risk-reducing overlay for the cross-asset momentum panel.

Constitutional invariant:
- ``RiskOverlay`` may ONLY reduce risk or postpone new entries.
- ``risk_scale`` and every per-symbol scale are clamped to ``[0.0, 1.0]`` on
  construction. Passing ``risk_scale=2.0`` yields ``1.0``.
- It can never increase exposure above the panel's ``risk_per_trade_pct`` /
  ``max_gross_exposure_mult`` dial, never bypass the $50 equity floor, never
  override an intrabar trailing stop, and never survive a ``HALT`` file.

Pre-registered rules:
1. Scheduled FOMC rate decision or US CPI release within the next 24h:
   ``block_new_entries = True`` (existing holdings keep their positions and
   stops; fresh entries wait until the 24h event window clears).
2. Crypto Fear & Greed Index >= 90 -> ``risk_scale = 0.25``;
   >= 80 -> ``risk_scale = 0.50``.
3. Symbol's 8h perpetual funding rate in the top decile (>= 90th percentile of
   its trailing 1-year distribution, min 90 observations) -> symbol scale 0.50x.
4. Fear & Greed <= 10 -> explicitly NO boost (``risk_scale`` stays 1.0).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd

from vito.signals.sources.calendar import events_within_window
from vito.signals.sources.feargreed import fear_greed_at
from vito.signals.sources.funding import funding_percentile_at


def _clamp_unit(val: float) -> float:
    if not math.isfinite(val):
        return 1.0
    return max(0.0, min(1.0, float(val)))


@dataclass(frozen=True)
class RiskOverlay:
    risk_scale: float = 1.0
    symbol_scales: dict[str, float] = field(default_factory=dict)
    block_new_entries: bool = False
    veto_symbols: frozenset[str] = frozenset()
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "risk_scale", _clamp_unit(self.risk_scale))
        cleaned = {str(k): _clamp_unit(v) for k, v in (self.symbol_scales or {}).items()}
        object.__setattr__(self, "symbol_scales", cleaned)
        object.__setattr__(self, "veto_symbols", frozenset(self.veto_symbols or ()))
        object.__setattr__(self, "reasons", tuple(self.reasons or ()))

    def scale_for(self, sym: str) -> float:
        """Effective exposure multiplier for ``sym`` in ``[0.0, 1.0]``."""
        if sym in self.veto_symbols:
            return 0.0
        sym_mult = self.symbol_scales.get(sym, 1.0)
        return _clamp_unit(self.risk_scale * sym_mult)


def evaluate_overlay(
    ts_ms: int,
    *,
    fear_greed: float | None = None,
    funding_pct: dict[str, float | None] | None = None,
    check_calendar: bool = True,
    calendar_hours_ahead: float = 24.0,
    veto_symbols: frozenset[str] | set[str] | None = None,
) -> RiskOverlay:
    """Evaluate the 4 pre-registered risk-overlay rules at ``ts_ms``."""
    risk_scale = 1.0
    symbol_scales: dict[str, float] = {}
    block_new = False
    reasons: list[str] = []

    # Rule 1: FOMC or CPI within the next 24h -> postpone new entries
    if check_calendar:
        upcoming = events_within_window(int(ts_ms), hours_ahead=calendar_hours_ahead)
        if upcoming:
            block_new = True
            for ev in upcoming:
                reasons.append(f"{ev.kind} within {int(calendar_hours_ahead)}h ({ev.iso_utc})")

    # Rule 2: Extreme Greed dampener (Rule 4: low F&G never boosts > 1.0)
    if fear_greed is not None and math.isfinite(fear_greed):
        if fear_greed >= 90.0:
            risk_scale = min(risk_scale, 0.25)
            reasons.append(f"Fear & Greed {fear_greed:.0f} >= 90 (0.25x scale)")
        elif fear_greed >= 80.0:
            risk_scale = min(risk_scale, 0.50)
            reasons.append(f"Fear & Greed {fear_greed:.0f} >= 80 (0.50x scale)")

    # Rule 3: Perpetual funding crowding in top decile (>= p90 over trailing 1y)
    if funding_pct:
        for sym in sorted(funding_pct):
            pct = funding_pct[sym]
            if pct is not None and math.isfinite(pct) and pct >= 90.0:
                symbol_scales[sym] = 0.50
                reasons.append(f"{sym} funding p{pct:.0f} >= p90 (0.50x symbol scale)")

    vetoes = frozenset(veto_symbols or ())
    for sym in sorted(vetoes):
        reasons.append(f"{sym} vetoed")

    return RiskOverlay(
        risk_scale=risk_scale,
        symbol_scales=symbol_scales,
        block_new_entries=block_new,
        veto_symbols=vetoes,
        reasons=tuple(reasons),
    )


def build_panel_overlay_fn(
    *,
    fng_series: pd.Series | None = None,
    funding_series: dict[str, pd.Series] | None = None,
    check_calendar: bool = True,
    calendar_hours_ahead: float = 24.0,
) -> Callable[[int], RiskOverlay]:
    """Build a point-in-time callable ``overlay_fn(ts_ms) -> RiskOverlay``."""
    fng = fng_series if fng_series is not None else pd.Series(dtype=float)
    fund_map = funding_series or {}

    def _fn(ts_ms: int) -> RiskOverlay:
        fg_val = fear_greed_at(fng, ts_ms)
        f_pcts: dict[str, float | None] = {
            sym: funding_percentile_at(series, ts_ms)
            for sym, series in fund_map.items()
        }
        return evaluate_overlay(
            ts_ms,
            fear_greed=fg_val,
            funding_pct=f_pcts,
            check_calendar=check_calendar,
            calendar_hours_ahead=calendar_hours_ahead,
        )

    return _fn
