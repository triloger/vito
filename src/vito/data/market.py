"""Market context: price, funding, and the cost of holding a position."""

from __future__ import annotations

from dataclasses import dataclass

from vito.exchange.okx import ExchangeUnavailable, OkxGateway


@dataclass(frozen=True, slots=True)
class MarketRow:
    inst_id: str
    last: float
    change_24h_pct: float
    spread_pct: float
    funding_rate_pct: float
    funding_apr_pct: float
    next_funding_ms: int


def market_row(gateway: OkxGateway, inst_id: str) -> MarketRow:
    ticker = gateway.ticker(inst_id)
    funding = gateway.funding(inst_id)
    return MarketRow(
        inst_id=inst_id,
        last=ticker.last,
        change_24h_pct=ticker.change_24h_pct,
        spread_pct=ticker.spread_pct,
        funding_rate_pct=funding.rate * 100,
        funding_apr_pct=funding.apr_pct,
        next_funding_ms=funding.next_funding_ms,
    )


def market_rows(gateway: OkxGateway, inst_ids: tuple[str, ...] | list[str]) -> list[MarketRow]:
    """Fetch rows for each instrument. Unavailable instruments are skipped.

    A dashboard should degrade, not explode: if one symbol fails, the rest
    still render.
    """
    rows: list[MarketRow] = []
    for inst_id in inst_ids:
        try:
            rows.append(market_row(gateway, inst_id))
        except ExchangeUnavailable:
            continue
    return rows
