"""Market Clock — US equity cash session, Binance 8h funding, and evaluation timers.

Why this exists
---------------
Binance's ``TRADIFI_PERPETUAL`` contracts (``QQQ-USDT-SWAP``, ``SPY-USDT-SWAP``,
``XAG-USDT-SWAP``) trade 24/7 on a synthetic book, while the underlying US ETFs
trade ``09:30–16:00 America/New_York`` Monday–Friday. Evaluating TradFi trend
signals at ``21:15 UTC`` (immediately after the US cash close) and enforcing
``STOP_MARKET`` orders on the exchange matching engine 24/7 avoids stale
overnight closes and unmonitored gap risk.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass
from typing import Any
from zoneinfo import ZoneInfo

from vito.data.instruments import asset_group
from vito.signals.sources.calendar import (
    calendar_digest,
    events_within_window,
    next_macro_event,
)

NY_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class InstrumentClockRow:
    inst_id: str
    family: str
    venue_book: str
    underlying_session: str
    liquid_window: bool


@dataclass(frozen=True, slots=True)
class MarketClockSnapshot:
    timestamp_ms: int
    iso_utc: str
    iso_ny: str
    us_session: str  # US_CASH_OPEN | PRE_MARKET | AFTER_HOURS | OVERNIGHT | WEEKEND_CLOSED
    us_cash_open: bool
    minutes_to_us_open: float
    minutes_to_us_close: float
    minutes_to_funding_8h: float
    next_funding_iso_utc: str
    funding_guard_active: bool
    minutes_to_tradfi_eval_2115utc: float
    minutes_to_crypto_eval_0012utc: float
    next_macro_event: dict[str, Any] | None
    macro_within_24h: bool
    calendar_digest: str
    instruments: tuple[InstrumentClockRow, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _next_daily_utc_time(now_utc: dt.datetime, hour: int, minute: int) -> dt.datetime:
    target = now_utc.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_utc:
        target += dt.timedelta(days=1)
    return target


def _next_funding_utc(now_utc: dt.datetime) -> dt.datetime:
    base_day = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    for h in (0, 8, 16, 24):
        cand = base_day + dt.timedelta(hours=h)
        if cand > now_utc:
            return cand
    return base_day + dt.timedelta(days=1)


def _us_session_state(now_ny: dt.datetime) -> tuple[str, bool, dt.datetime, dt.datetime]:
    """Return ``(session_label, is_cash_open, next_open_ny, next_close_ny)``."""
    wd = now_ny.weekday()  # 0=Mon .. 6=Sun
    t_min = now_ny.hour * 60 + now_ny.minute
    open_min = 9 * 60 + 30
    close_min = 16 * 60

    if wd >= 5:
        session = "WEEKEND_CLOSED"
        is_open = False
    elif open_min <= t_min < close_min:
        session = "US_CASH_OPEN"
        is_open = True
    elif 4 * 60 <= t_min < open_min:
        session = "PRE_MARKET"
        is_open = False
    elif close_min <= t_min < 20 * 60:
        session = "AFTER_HOURS"
        is_open = False
    else:
        session = "OVERNIGHT"
        is_open = False

    # Find next 09:30 ET on a weekday
    cand_open = now_ny.replace(hour=9, minute=30, second=0, microsecond=0)
    if cand_open <= now_ny or cand_open.weekday() >= 5:
        cand_open += dt.timedelta(days=1)
        while cand_open.weekday() >= 5:
            cand_open += dt.timedelta(days=1)

    # Find next 16:00 ET on a weekday
    cand_close = now_ny.replace(hour=16, minute=0, second=0, microsecond=0)
    if cand_close <= now_ny or cand_close.weekday() >= 5:
        cand_close += dt.timedelta(days=1)
        while cand_close.weekday() >= 5:
            cand_close += dt.timedelta(days=1)

    return session, is_open, cand_open, cand_close


def evaluate_market_clock(
    now_ms: int,
    *,
    symbols: tuple[str, ...] | list[str] = (),
    funding_guard_minutes: int = 15,
) -> MarketClockSnapshot:
    """Compute a deterministic point-in-time Market Clock snapshot."""
    now_utc = dt.datetime.fromtimestamp(now_ms / 1000.0, tz=dt.UTC)
    now_ny = now_utc.astimezone(NY_TZ)

    us_session, us_open, next_open_ny, next_close_ny = _us_session_state(now_ny)
    mins_to_open = round((next_open_ny - now_ny).total_seconds() / 60.0, 1)
    mins_to_close = round((next_close_ny - now_ny).total_seconds() / 60.0, 1)

    next_fund = _next_funding_utc(now_utc)
    mins_to_fund = round((next_fund - now_utc).total_seconds() / 60.0, 1)
    # Also check minutes since last funding (8h = 480m)
    mins_since_fund = round(480.0 - mins_to_fund, 1)
    guard_active = (
        mins_to_fund <= funding_guard_minutes or mins_since_fund <= funding_guard_minutes
    )

    next_2115 = _next_daily_utc_time(now_utc, 21, 15)
    next_0012 = _next_daily_utc_time(now_utc, 0, 12)
    mins_2115 = round((next_2115 - now_utc).total_seconds() / 60.0, 1)
    mins_0012 = round((next_0012 - now_utc).total_seconds() / 60.0, 1)

    next_ev = next_macro_event(now_ms)
    upcoming_24h = events_within_window(now_ms, hours_ahead=24.0)

    inst_rows: list[InstrumentClockRow] = []
    for sym in symbols:
        fam = asset_group(sym)
        is_swap = sym.upper().endswith("-SWAP")
        venue_book = "24/7 Perp Swap" if is_swap else "24/7 Spot"
        if fam in ("equity_index", "pre_ipo"):
            under_sess = (
                "US Cash Open (primary liquidity)"
                if us_open
                else f"US {us_session} (synthetic 24/7 perp)"
            )
            liq = us_open or us_session in ("PRE_MARKET", "AFTER_HOURS")
        elif fam in ("precious_metals", "industrial_metals", "energy"):
            under_sess = (
                "Global OTC/COMEX Open"
                if now_ny.weekday() < 5
                else "Weekend (synthetic 24/7 book)"
            )
            liq = now_ny.weekday() < 5 or not is_swap
        else:
            under_sess = "24/7 Native Crypto"
            liq = True
        inst_rows.append(
            InstrumentClockRow(
                inst_id=sym,
                family=fam,
                venue_book=venue_book,
                underlying_session=under_sess,
                liquid_window=liq,
            )
        )

    return MarketClockSnapshot(
        timestamp_ms=now_ms,
        iso_utc=now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        iso_ny=now_ny.strftime("%Y-%m-%dT%H:%M:%S%z"),
        us_session=us_session,
        us_cash_open=us_open,
        minutes_to_us_open=mins_to_open,
        minutes_to_us_close=mins_to_close,
        minutes_to_funding_8h=mins_to_fund,
        next_funding_iso_utc=next_fund.strftime("%Y-%m-%dT%H:%M:%SZ"),
        funding_guard_active=guard_active,
        minutes_to_tradfi_eval_2115utc=mins_2115,
        minutes_to_crypto_eval_0012utc=mins_0012,
        next_macro_event=asdict(next_ev) if next_ev is not None else None,
        macro_within_24h=bool(upcoming_24h),
        calendar_digest=calendar_digest(),
        instruments=tuple(inst_rows),
    )
