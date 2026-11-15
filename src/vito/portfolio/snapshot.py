"""Account snapshot: what you hold, what it is worth, how it is allocated."""

from __future__ import annotations

from dataclasses import dataclass

from vito.exchange.okx import OkxGateway


@dataclass(frozen=True, slots=True)
class Holding:
    currency: str
    free: float
    used: float
    total: float
    usdt_value: float
    priced: bool


@dataclass(frozen=True, slots=True)
class Snapshot:
    quote: str
    holdings: tuple[Holding, ...]
    total_usdt: float
    cash_usdt: float

    @property
    def stable_share_pct(self) -> float:
        return self.cash_usdt / self.total_usdt * 100 if self.total_usdt else 0.0


def build_snapshot(gateway: OkxGateway, quote: str = "USDT") -> Snapshot:
    """Value every non-zero balance in ``quote``.

    Unpriced balances are reported with ``priced=False`` rather than silently
    counted as zero, so the total is never quietly wrong.
    """
    holdings: list[Holding] = []
    total = 0.0
    cash = 0.0

    for entry in gateway.balances().values():
        price = gateway.spot_price(entry.currency, quote)
        priced = price is not None
        value = entry.total * (price or 0.0)
        holdings.append(
            Holding(
                currency=entry.currency,
                free=entry.free,
                used=entry.used,
                total=entry.total,
                usdt_value=value,
                priced=priced,
            )
        )
        if priced:
            total += value
            if entry.currency == quote:
                cash += value

    holdings.sort(key=lambda h: h.usdt_value, reverse=True)
    return Snapshot(
        quote=quote,
        holdings=tuple(holdings),
        total_usdt=total,
        cash_usdt=cash,
    )
