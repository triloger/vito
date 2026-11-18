"""Binance connectivity (USDⓈ-M Futures + Spot).

Provides the same interface as :class:`vito.exchange.okx.OkxGateway` while
preserving Vito's canonical instrument IDs (``BTC-USDT-SWAP`` for linear USDT
perpetuals and ``BTC-USDT`` for spot) so strategies, policy, backtests, and the
paper broker are venue-agnostic.
"""

from __future__ import annotations

import contextlib
from typing import Any

import ccxt

from vito.config import Credentials
from vito.exchange.okx import (
    BalanceEntry,
    ExchangeUnavailable,
    Funding,
    MissingCredentials,
    Ticker,
    _interval_hours,
)


def canonical_inst_id(market: dict[str, Any]) -> str:
    """Return Vito's canonical ``<BASE>-USDT-SWAP`` or ``<BASE>-USDT`` ID."""
    raw_id = str(market.get("id") or "")
    if "-" in raw_id:
        return raw_id
    base = str(market.get("base") or "").upper()
    quote = str(market.get("quote") or "USDT").upper()
    m_type = str(market.get("type") or "")
    if m_type == "swap" and base and quote:
        return f"{base}-{quote}-SWAP"
    if m_type == "spot" and base and quote:
        return f"{base}-{quote}"
    return raw_id


class BinanceGateway:
    """Credential-aware Binance client (USDⓈ-M linear perpetuals + Spot)."""

    venue: str = "binance"

    def __init__(self, credentials: Credentials | None = None, *, timeout_ms: int = 15_000):
        self.credentials = credentials or Credentials(venue="binance")
        self._markets: dict[str, dict] | None = None
        self._by_inst_id: dict[str, dict] = {}

        base_params: dict[str, Any] = {
            "enableRateLimit": True,
            "timeout": timeout_ms,
        }
        if self.has_credentials:
            base_params.update(
                apiKey=self.credentials.api_key,
                secret=self.credentials.api_secret,
            )

        fut_params = {**base_params, "options": {"defaultType": "future"}}
        spot_params = {**base_params, "options": {"defaultType": "spot"}}

        self._client = ccxt.binance(fut_params)
        self._spot_client = ccxt.binance(spot_params)
        if self.credentials.demo:
            with contextlib.suppress(Exception):
                self._client.set_sandbox_mode(True)
                self._spot_client.set_sandbox_mode(True)

    @property
    def has_credentials(self) -> bool:
        return bool(self.credentials.api_key and self.credentials.api_secret)

    @property
    def client(self) -> ccxt.binance:
        """Primary USDⓈ-M futures ccxt client."""
        return self._client

    @property
    def spot_client(self) -> ccxt.binance:
        """Spot ccxt client."""
        return self._spot_client

    # -- public market data ------------------------------------------------

    def markets(self) -> dict[str, dict]:
        if self._markets is None:
            try:
                fut_markets = self._client.load_markets() or {}
            except ccxt.BaseError as exc:  # pragma: no cover - network
                raise ExchangeUnavailable(str(exc)) from exc
            merged: dict[str, dict] = {}
            by_id: dict[str, dict] = {}
            for sym, m in fut_markets.items():
                if m.get("type") == "swap" and m.get("linear") and m.get("quote") == "USDT":
                    cid = canonical_inst_id(m)
                    merged[sym] = m
                    by_id[cid] = m
                    by_id[str(m.get("id") or "")] = m
                    by_id[sym] = m
            try:
                spot_markets = self._spot_client.load_markets() or {}
                for sym, m in spot_markets.items():
                    if m.get("type") == "spot" and m.get("quote") == "USDT":
                        cid = canonical_inst_id(m)
                        merged[sym] = m
                        by_id.setdefault(cid, m)
                        by_id.setdefault(sym, m)
            except ccxt.BaseError:
                pass
            self._markets = merged
            self._by_inst_id = by_id
        return self._markets

    def _market(self, inst_id: str) -> dict:
        self.markets()
        if inst_id in self._by_inst_id:
            return self._by_inst_id[inst_id]
        upper = inst_id.upper()
        if upper in self._by_inst_id:
            return self._by_inst_id[upper]
        if upper.endswith("-USDT-SWAP"):
            sym = f"{upper[:-10]}/USDT:USDT"
            if sym in self._by_inst_id:
                return self._by_inst_id[sym]
        if upper.endswith("-USDT"):
            sym = f"{upper[:-5]}/USDT"
            if sym in self._by_inst_id:
                return self._by_inst_id[sym]
        raise ExchangeUnavailable(f"unknown instrument: {inst_id}")

    def _client_for(self, market: dict) -> ccxt.binance:
        return self._spot_client if market.get("type") == "spot" else self._client

    def ticker(self, inst_id: str) -> Ticker:
        market = self._market(inst_id)
        cli = self._client_for(market)
        try:
            raw = cli.fetch_ticker(market["symbol"])
        except ccxt.BaseError as exc:  # pragma: no cover - network
            raise ExchangeUnavailable(str(exc)) from exc
        last = float(raw.get("last") or raw.get("close") or 0)
        bid = float(raw.get("bid") or 0)
        ask = float(raw.get("ask") or 0)
        # Binance /fapi/v1/ticker/24hr omits bid/ask on some endpoints; fall back to bookTicker
        if (not bid or not ask) and last > 0:
            try:
                ob = cli.fetch_order_book(market["symbol"], limit=5)
                if ob.get("bids"):
                    bid = float(ob["bids"][0][0])
                if ob.get("asks"):
                    ask = float(ob["asks"][0][0])
            except Exception:  # noqa: BLE001
                bid = bid or last
                ask = ask or last
        return Ticker(
            inst_id=canonical_inst_id(market),
            last=last,
            bid=bid,
            ask=ask,
            change_24h_pct=float(raw.get("percentage") or 0),
            high_24h=float(raw.get("high") or 0),
            low_24h=float(raw.get("low") or 0),
            volume_24h=float(raw.get("baseVolume") or 0),
        )

    def funding(self, inst_id: str) -> Funding:
        market = self._market(inst_id)
        if market.get("type") != "swap":
            return Funding(
                inst_id=canonical_inst_id(market),
                rate=0.0,
                interval_hours=8.0,
                next_funding_ms=0,
            )
        try:
            raw = self._client.fetch_funding_rate(market["symbol"])
        except ccxt.BaseError as exc:  # pragma: no cover - network
            raise ExchangeUnavailable(str(exc)) from exc
        return Funding(
            inst_id=canonical_inst_id(market),
            rate=float(raw.get("fundingRate") or 0),
            interval_hours=_interval_hours(raw.get("interval")),
            next_funding_ms=int(raw.get("fundingTimestamp") or raw.get("nextFundingTime") or 0),
        )

    def min_notional(self, inst_id: str) -> float:
        """Effective minimum order notional in USDT on Binance."""
        market = self._market(inst_id)
        try:
            price = self.ticker(inst_id).mid
        except ExchangeUnavailable:  # pragma: no cover - network
            price = 0.0
        contract = float(market.get("contractSize") or 1.0)
        limits = market.get("limits") or {}
        min_amount = float((limits.get("amount") or {}).get("min") or 0.0)
        min_cost = float((limits.get("cost") or {}).get("min") or 0.0)
        return max(contract * min_amount * price, min_cost)

    def swap_tickers(self) -> dict[str, dict[str, Any]]:
        """Fetch all linear swap tickers including bid/ask from bookTicker."""
        try:
            tickers = self._client.fetch_tickers()
            bids_asks = self._client.fapiPublicGetTickerBookTicker()
        except ccxt.BaseError as exc:  # pragma: no cover - network
            raise ExchangeUnavailable(str(exc)) from exc
        ba_map = {
            str(item.get("symbol") or ""): (
                float(item.get("bidPrice") or 0.0),
                float(item.get("askPrice") or 0.0),
            )
            for item in (bids_asks if isinstance(bids_asks, list) else [])
        }
        for m in self.markets().values():
            if m.get("type") != "swap":
                continue
            sym = str(m.get("symbol") or "")
            raw_id = str(m.get("id") or "")
            if sym in tickers and raw_id in ba_map:
                bid, ask = ba_map[raw_id]
                if bid > 0:
                    tickers[sym]["bid"] = bid
                if ask > 0:
                    tickers[sym]["ask"] = ask
        return tickers

    def spot_tickers(self) -> dict[str, dict[str, Any]]:
        try:
            return self._spot_client.fetch_tickers()
        except ccxt.BaseError as exc:  # pragma: no cover - network
            raise ExchangeUnavailable(str(exc)) from exc

    # -- private: read-only -------------------------------------------------

    def balances(self) -> dict[str, BalanceEntry]:
        """Read non-zero balances across Futures, Spot, Funding, and Simple Earn."""
        if not self.has_credentials:
            raise MissingCredentials(
                "account access needs VITO_BINANCE_API_KEY and VITO_BINANCE_API_SECRET"
            )
        entries: dict[str, BalanceEntry] = {}
        errors: list[str] = []

        def _add(currency: str, free_f: float, used_f: float, total_f: float) -> None:
            if total_f <= 1e-7 and free_f <= 1e-7 and used_f <= 1e-7:
                return
            prev = entries.get(currency)
            if prev is None:
                entries[currency] = BalanceEntry(currency, free_f, used_f, total_f)
            else:
                entries[currency] = BalanceEntry(
                    currency,
                    prev.free + free_f,
                    prev.used + used_f,
                    prev.total + total_f,
                )

        for cli, params in (
            (self._client, {}),
            (self._spot_client, {"type": "spot"}),
            (self._spot_client, {"type": "funding"}),
        ):
            try:
                raw = cli.fetch_balance(params)
            except ccxt.BaseError as exc:  # pragma: no cover - network
                errors.append(str(exc))
                continue
            for currency, total in (raw.get("total") or {}).items():
                # Skip Binance internal Simple Earn share tokens (LD*); real amounts
                # are fetched below from /sapi/v1/simple-earn/flexible/position.
                if str(currency).startswith("LD") and len(str(currency)) > 2:
                    continue
                total_f = float(total or 0)
                free_f = float((raw.get("free") or {}).get(currency) or 0)
                used_f = float((raw.get("used") or {}).get(currency) or 0)
                _add(str(currency), free_f, used_f, total_f)

        # Include Simple Earn Flexible & Locked balances at their true underlying value
        with contextlib.suppress(Exception):
            flex = self._spot_client.sapiGetSimpleEarnFlexiblePosition()
            for row in (flex or {}).get("rows") or []:
                ccy = str(row.get("asset") or "")
                amt = float(row.get("totalAmount") or 0.0)
                if ccy and amt > 0:
                    _add(ccy, amt, 0.0, amt)

        with contextlib.suppress(Exception):
            locked = self._spot_client.sapiGetSimpleEarnLockedPosition()
            for row in (locked or {}).get("rows") or []:
                ccy = str(row.get("asset") or "")
                amt = float(row.get("amount") or 0.0)
                if ccy and amt > 0:
                    _add(ccy, 0.0, amt, amt)

        if not entries and len(errors) >= 2:
            raise ExchangeUnavailable(errors[0])
        return entries

    def spot_price(self, currency: str, quote: str = "USDT") -> float | None:
        if currency == quote:
            return 1.0
        for cli, sym in (
            (self._spot_client, f"{currency}/{quote}"),
            (self._client, f"{currency}/{quote}:{quote}"),
        ):
            try:
                return float(cli.fetch_ticker(sym)["last"])
            except Exception:  # noqa: BLE001
                continue
        return None

    # -- lifecycle ---------------------------------------------------------

    def ping(self) -> bool:
        try:
            self._client.fetch_time()
            return True
        except ccxt.BaseError:  # pragma: no cover - network
            return False

    def close(self) -> None:  # pragma: no cover - trivial
        for cli in (self._client, self._spot_client):
            with contextlib.suppress(Exception):
                cli.close()
