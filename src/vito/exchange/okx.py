"""OKX connectivity.

One thin wrapper around ccxt. Public market data needs no credentials; account
access requires keys. The same object is used by the read-only dashboard today
and by the order router later, so the plumbing is proven on the safe path
first.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

import ccxt

from vito.config import Credentials


class MissingCredentials(RuntimeError):
    """Raised when a private endpoint is called without API keys."""


class ExchangeUnavailable(RuntimeError):
    """Raised when OKX cannot be reached or returns something unusable."""


@dataclass(frozen=True, slots=True)
class Ticker:
    inst_id: str
    last: float
    bid: float
    ask: float
    change_24h_pct: float
    high_24h: float
    low_24h: float
    volume_24h: float

    @property
    def spread_pct(self) -> float:
        if not (self.bid and self.ask):
            return 0.0
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 100 if mid else 0.0

    @property
    def mid(self) -> float:
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2
        return self.last


@dataclass(frozen=True, slots=True)
class Funding:
    inst_id: str
    rate: float  # per funding interval, as a fraction
    interval_hours: float
    next_funding_ms: int

    @property
    def apr_pct(self) -> float:
        """Annualised funding cost, in percent. Negative means shorts pay longs."""
        if self.interval_hours <= 0:
            return 0.0
        periods_per_year = 24 / self.interval_hours * 365
        return self.rate * periods_per_year * 100


@dataclass(frozen=True, slots=True)
class BalanceEntry:
    currency: str
    free: float
    used: float
    total: float


class OkxGateway:
    """Credential-aware OKX client.

    ``has_credentials`` is false for a public-only client, and every private
    method raises :class:`MissingCredentials` in that case rather than failing
    with an opaque exchange error.
    """

    venue: str = "okx"

    def __init__(self, credentials: Credentials | None = None, *, timeout_ms: int = 15_000):
        self.credentials = credentials or Credentials()
        self._markets: dict[str, dict] | None = None

        params: dict = {
            "enableRateLimit": True,
            "timeout": timeout_ms,
            "options": {"defaultType": "swap"},
        }
        if self.credentials.complete:
            params.update(
                apiKey=self.credentials.api_key,
                secret=self.credentials.api_secret,
                password=self.credentials.passphrase,
            )

        self._client = ccxt.okx(params)
        if self.credentials.demo:
            self._client.set_sandbox_mode(True)

    # -- properties --------------------------------------------------------

    @property
    def has_credentials(self) -> bool:
        return self.credentials.complete

    @property
    def client(self) -> ccxt.okx:
        """Escape hatch for research code. Not for use by the order router."""
        return self._client

    # -- public market data ------------------------------------------------

    def markets(self) -> dict[str, dict]:
        if self._markets is None:
            try:
                self._client.load_markets()
            except ccxt.BaseError as exc:  # pragma: no cover - network
                raise ExchangeUnavailable(str(exc)) from exc
            self._markets = self._client.markets or {}
        return self._markets

    def _market(self, inst_id: str) -> dict:
        for market in self.markets().values():
            if market.get("id") == inst_id:
                return market
        raise ExchangeUnavailable(f"unknown instrument: {inst_id}")

    def ticker(self, inst_id: str) -> Ticker:
        market = self._market(inst_id)
        try:
            raw = self._client.fetch_ticker(market["symbol"])
        except ccxt.BaseError as exc:  # pragma: no cover - network
            raise ExchangeUnavailable(str(exc)) from exc
        return Ticker(
            inst_id=inst_id,
            last=float(raw.get("last") or 0),
            bid=float(raw.get("bid") or 0),
            ask=float(raw.get("ask") or 0),
            change_24h_pct=float(raw.get("percentage") or 0),
            high_24h=float(raw.get("high") or 0),
            low_24h=float(raw.get("low") or 0),
            volume_24h=float(raw.get("baseVolume") or 0),
        )

    def funding(self, inst_id: str) -> Funding:
        market = self._market(inst_id)
        try:
            raw = self._client.fetch_funding_rate(market["symbol"])
        except ccxt.BaseError as exc:  # pragma: no cover - network
            raise ExchangeUnavailable(str(exc)) from exc
        return Funding(
            inst_id=inst_id,
            rate=float(raw.get("fundingRate") or 0),
            interval_hours=_interval_hours(raw.get("interval")),
            next_funding_ms=int(raw.get("fundingTimestamp") or raw.get("nextFundingTime") or 0),
        )

    def min_notional(self, inst_id: str) -> float:
        """Minimum order value in USDT, derived from contract size and lot step."""
        market = self._market(inst_id)
        try:
            price = self.ticker(inst_id).mid
        except ExchangeUnavailable:  # pragma: no cover - network
            price = 0.0
        contract = float(market.get("contractSize") or 1)
        min_amount = float(market["limits"]["amount"]["min"] or 0)
        return contract * min_amount * price

    # -- private: read-only -------------------------------------------------

    def balances(self) -> dict[str, BalanceEntry]:
        if not self.has_credentials:
            raise MissingCredentials(
                "account access needs VITO_OKX_API_KEY / SECRET / PASSPHRASE"
            )
        try:
            raw = self._client.fetch_balance()
        except ccxt.BaseError as exc:  # pragma: no cover - network
            raise ExchangeUnavailable(str(exc)) from exc

        entries: dict[str, BalanceEntry] = {}
        for currency, total in (raw.get("total") or {}).items():
            total = float(total or 0)
            free = float((raw.get("free") or {}).get(currency) or 0)
            used = float((raw.get("used") or {}).get(currency) or 0)
            if total or free or used:
                entries[currency] = BalanceEntry(currency, free, used, total)
        return entries

    def spot_price(self, currency: str, quote: str = "USDT") -> float | None:
        """Best-effort spot price of ``currency`` in ``quote``."""
        if currency == quote:
            return 1.0
        symbol = f"{currency}/{quote}"
        try:
            return float(self._client.fetch_ticker(symbol)["last"])
        except Exception:  # noqa: BLE001 - valuation is best-effort
            return None

    # -- lifecycle ---------------------------------------------------------

    def ping(self) -> bool:
        try:
            self._client.fetch_time()
            return True
        except ccxt.BaseError:  # pragma: no cover - network
            return False

    def close(self) -> None:  # pragma: no cover - trivial
        with contextlib.suppress(Exception):
            self._client.close()


def _interval_hours(interval: str | None) -> float:
    """Parse ccxt funding intervals such as ``8h`` or ``1h``."""
    if not interval:
        return 8.0
    digits = "".join(ch for ch in interval if ch.isdigit() or ch == ".")
    try:
        value = float(digits)
    except ValueError:
        return 8.0
    return value / 60 if interval.strip().endswith("m") else value or 8.0
