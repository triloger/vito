"""Exchange connectivity."""

from __future__ import annotations

from vito.config import Credentials, load_credentials
from vito.exchange.binance import BinanceGateway, canonical_inst_id
from vito.exchange.okx import (
    BalanceEntry,
    ExchangeUnavailable,
    Funding,
    MissingCredentials,
    OkxGateway,
    Ticker,
)

Gateway = BinanceGateway | OkxGateway


def build_gateway(
    credentials: Credentials | None = None, *, timeout_ms: int = 15_000
) -> Gateway:
    """Instantiate the configured exchange gateway (defaults to Binance)."""
    creds = credentials if credentials is not None else load_credentials()
    if creds.venue == "okx":
        return OkxGateway(creds, timeout_ms=timeout_ms)
    return BinanceGateway(creds, timeout_ms=timeout_ms)


__all__ = [
    "BalanceEntry",
    "BinanceGateway",
    "ExchangeUnavailable",
    "Funding",
    "Gateway",
    "MissingCredentials",
    "OkxGateway",
    "Ticker",
    "build_gateway",
    "canonical_inst_id",
]
