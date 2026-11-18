"""Instrument catalog and tradeability gate.

Before testing a strategy on an instrument, two physical constraints must hold:

1. **Order size fits the account.** On a $200 account with a 1.5% risk budget
   and a 2% stop, target notional is $150 (0.75x equity). If the exchange's
   minimum order (`max(contractSize * minSz * price, minCost)`) exceeds the
   position cap, or forces effective exposure above the ceiling, the instrument
   is untradeable at this account size.
2. **The book is liquid enough that backtest fills are not fiction.** Thin
   books with wide spreads turn historical close-to-open backtests into wishful
   thinking. A hard liquidity gate (`volume_24h_usdt >= 100_000` and
   `spread_bps <= 10.0`) filters out dead pairs before research begins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import ccxt

from vito.exchange.binance import canonical_inst_id
from vito.exchange.okx import ExchangeUnavailable

INST_CATEGORY_MAP = {
    "1": "crypto",
    "3": "equity",
    "4": "commodity",
}

COMMODITY_BASES = {
    "XAU",
    "XAUT",
    "PAXG",
    "XAG",
    "XPT",
    "XPD",
    "XCU",
    "COPPER",
    "CL",
    "BZ",
    "NG",
    "NATGAS",
    "H100",
    "USO",
    "XUSO",
    "URNM",
    "XURNM",
}

PRECIOUS_METALS_BASES = {"PAXG", "XAU", "XAUT", "XAG", "XPT", "XPD", "GDX"}
INDUSTRIAL_METALS_BASES = {"COPPER", "XCU", "URNM", "XURNM", "CCJ", "MP", "CRML"}
ENERGY_BASES = {"CL", "BZ", "NATGAS", "NG", "USO", "XUSO", "XLE", "XOM", "OXY", "EQT"}
EQUITY_INDEX_BASES = {
    "QQQ",
    "SPY",
    "IWM",
    "SMH",
    "SOXX",
    "EWZ",
    "EWJ",
    "EWY",
    "EWT",
    "TQQQ",
    "SQQQ",
    "KODEX200",
}
PRE_IPO_BASES = {"OPENAI", "ANTHROPIC", "SPCX", "MINIMAX", "ZHIPU"}


def asset_group(inst_id_or_base: str) -> str:
    """Return correlation family for group-capped portfolio construction."""
    raw = str(inst_id_or_base).upper()
    base = raw.split("-")[0].split("/")[0]
    if base in PRECIOUS_METALS_BASES:
        return "precious_metals"
    if base in INDUSTRIAL_METALS_BASES:
        return "industrial_metals"
    if base in ENERGY_BASES:
        return "energy"
    if base in EQUITY_INDEX_BASES:
        return "equity_index"
    if base in PRE_IPO_BASES:
        return "pre_ipo"
    return "crypto"


def is_true_perpetual(market: dict[str, Any]) -> bool:
    """Return False if a swap market is settling, inactive, or has a near-term delivery date."""
    if not market.get("active", True):
        return False
    info = market.get("info") or {}
    status = str(info.get("status") or "").upper()
    if status and status not in ("TRADING", "LIVE"):
        return False
    if str(market.get("type") or "") == "swap":
        sym = str(market.get("symbol") or "")
        if ":" in sym and "-" in sym.split(":")[-1]:
            return False
        delivery = info.get("deliveryDate")
        if delivery is not None:
            try:
                if 0 < int(delivery) < 4_000_000_000_000:
                    return False
            except (TypeError, ValueError):
                pass
    return True

CRYPTO_X_BASES = {
    "XRP",
    "XLM",
    "XTZ",
    "XCH",
    "XPL",
    "XDP",
    "XEC",
    "XMR",
    "XEM",
    "XDC",
    "XAI",
    "XION",
    "XCN",
    "XVS",
    "XVG",
    "XNO",
    "XPRT",
}


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    inst_id: str
    symbol: str
    kind: str  # "swap" | "spot"
    asset_class: str  # "crypto" | "equity" | "commodity"
    active: bool
    last: float
    spread_bps: float
    volume_24h_usdt: float
    contract_size: float
    min_size: float
    tick_size: float
    min_notional_usdt: float
    maker_bps: float
    taker_bps: float
    max_leverage: float
    list_time_ms: int
    tradeable: bool
    reject_reason: str = ""


def classify_asset(market: dict[str, Any]) -> str:
    """Classify a Binance or OKX market into ``crypto``, ``equity``, or ``commodity``."""
    info = market.get("info") or {}
    cat = str(info.get("instCategory") or "")
    if cat in INST_CATEGORY_MAP:
        return INST_CATEGORY_MAP[cat]

    base = str(market.get("base") or "").upper()
    if base in COMMODITY_BASES:
        return "commodity"

    underlying = str(info.get("underlyingType") or "").upper()
    contract_type = str(info.get("contractType") or "").upper()
    subtypes = [str(s).upper() for s in (info.get("underlyingSubType") or [])]
    if underlying == "COMMODITY" or "COMMODITY" in subtypes:
        return "commodity"
    if underlying == "EQUITY" or contract_type == "TRADIFI_PERPETUAL" or "TRADFI" in subtypes:
        return "equity"

    if base.startswith("X") and base not in CRYPTO_X_BASES and len(base) > 1:
        return "equity"
    return "crypto"


def evaluate_market(
    market: dict[str, Any],
    ticker: dict[str, Any] | None,
    *,
    max_order_usdt: float = 150.0,
    min_volume_usdt: float = 100_000.0,
    max_spread_bps: float = 10.0,
) -> InstrumentSpec | None:
    """Build an :class:`InstrumentSpec` from a ccxt market and ticker dict."""
    kind = str(market.get("type") or "")
    if kind not in ("swap", "spot"):
        return None
    if kind == "swap" and not market.get("linear"):
        return None
    if str(market.get("quote") or "") != "USDT":
        return None

    inst_id = canonical_inst_id(market)
    symbol = str(market.get("symbol") or "")
    active = bool(market.get("active", True))
    info = market.get("info") or {}

    t = ticker or {}
    t_info = t.get("info") or {}
    last = float(t.get("last") or t.get("close") or 0.0)
    bid = float(t.get("bid") or 0.0)
    ask = float(t.get("ask") or 0.0)
    mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else last

    spread_bps = (
        (ask - bid) / mid * 10_000.0 if (bid > 0 and ask > 0 and mid > 0) else float("inf")
    )

    vol_ccy = float(t_info.get("volCcy24h") or 0.0)
    if vol_ccy > 0:
        volume_24h_usdt = vol_ccy * last if kind == "swap" else vol_ccy
    else:
        qvol = float(t.get("quoteVolume") or t_info.get("quoteVolume") or 0.0)
        bvol = float(t.get("baseVolume") or 0.0)
        contract_val = float(market.get("contractSize") or 1.0)
        volume_24h_usdt = qvol if qvol > 0 else bvol * contract_val * last

    contract_size = float(market.get("contractSize") or 1.0)
    limits = market.get("limits") or {}
    limits_amt = limits.get("amount") or {}
    limits_cost = limits.get("cost") or {}
    raw_min_sz = limits_amt.get("min") or info.get("minSz")
    raw_min_cost = limits_cost.get("min")
    try:
        min_size = float(raw_min_sz) if raw_min_sz is not None else 0.0
    except (TypeError, ValueError):
        min_size = 0.0
    try:
        min_cost = float(raw_min_cost) if raw_min_cost is not None else 0.0
    except (TypeError, ValueError):
        min_cost = 0.0
    if min_size != min_size:  # NaN check
        min_size = 0.0
    if min_cost != min_cost:  # NaN check
        min_cost = 0.0
    prec = market.get("precision") or {}
    tick_size = float(prec.get("price") or info.get("tickSz") or 0.0)

    ref_price = mid if mid > 0 else last
    lot_notional = contract_size * min_size * ref_price
    min_notional_usdt = max(lot_notional, min_cost) if ref_price > 0 else 0.0

    maker_bps = float(market.get("maker") or 0.0) * 10_000.0
    taker_bps = float(market.get("taker") or 0.0) * 10_000.0
    try:
        max_leverage = float(info.get("lever") or (1.0 if kind == "spot" else 20.0))
    except (TypeError, ValueError):
        max_leverage = 1.0 if kind == "spot" else 20.0

    try:
        list_time_ms = int(info.get("listTime") or info.get("onboardDate") or 0)
    except (TypeError, ValueError):
        list_time_ms = 0

    reasons: list[str] = []
    if not active:
        reasons.append("inactive")
    if not is_true_perpetual(market) and "inactive" not in reasons:
        reasons.append("settling_or_dated")
    if ref_price <= 0:
        reasons.append("unpriced")
    if min_notional_usdt <= 0 or min_notional_usdt > max_order_usdt:
        reasons.append(f"min_notional>{max_order_usdt:.0f}")
    if volume_24h_usdt < min_volume_usdt:
        reasons.append("illiquid")
    if spread_bps > max_spread_bps:
        reasons.append("wide_spread")

    return InstrumentSpec(
        inst_id=inst_id,
        symbol=symbol,
        kind=kind,
        asset_class=classify_asset(market),
        active=active,
        last=round(last, 6),
        spread_bps=round(spread_bps, 2) if spread_bps != float("inf") else 9999.0,
        volume_24h_usdt=round(volume_24h_usdt, 2),
        contract_size=contract_size,
        min_size=min_size,
        tick_size=tick_size,
        min_notional_usdt=round(min_notional_usdt, 2),
        maker_bps=round(maker_bps, 2),
        taker_bps=round(taker_bps, 2),
        max_leverage=max_leverage,
        list_time_ms=list_time_ms,
        tradeable=len(reasons) == 0,
        reject_reason=",".join(reasons),
    )


def scan_instruments(
    gateway: Any,
    *,
    kind: str = "all",
    asset_class: str = "all",
    equity: float = 200.0,
    max_exposure_mult: float = 0.75,
    min_volume_usdt: float = 100_000.0,
    max_spread_bps: float = 10.0,
    only_tradeable: bool = True,
) -> list[InstrumentSpec]:
    """Scan USDT spot and linear swap markets and apply the liquidity/sizing gate."""
    markets = gateway.markets()
    max_order_usdt = equity * max_exposure_mult

    swap_tickers: dict[str, dict[str, Any]] = {}
    spot_tickers: dict[str, dict[str, Any]] = {}
    try:
        if hasattr(gateway, "swap_tickers"):
            if kind in ("all", "swap"):
                swap_tickers = gateway.swap_tickers()
            if kind in ("all", "spot"):
                spot_tickers = gateway.spot_tickers()
        else:
            if kind in ("all", "swap"):
                swap_tickers = gateway.client.fetch_tickers(params={"instType": "SWAP"})
            if kind in ("all", "spot"):
                spot_tickers = gateway.client.fetch_tickers(params={"instType": "SPOT"})
    except ccxt.BaseError as exc:  # pragma: no cover - network
        raise ExchangeUnavailable(str(exc)) from exc

    specs: list[InstrumentSpec] = []
    for market in markets.values():
        m_type = str(market.get("type") or "")
        if kind != "all" and m_type != kind:
            continue
        sym = str(market.get("symbol") or "")
        ticker = swap_tickers.get(sym) if m_type == "swap" else spot_tickers.get(sym)
        spec = evaluate_market(
            market,
            ticker,
            max_order_usdt=max_order_usdt,
            min_volume_usdt=min_volume_usdt,
            max_spread_bps=max_spread_bps,
        )
        if spec is None:
            continue
        if asset_class != "all" and spec.asset_class != asset_class:
            continue
        if only_tradeable and not spec.tradeable:
            continue
        specs.append(spec)

    specs.sort(key=lambda s: (-s.volume_24h_usdt, s.inst_id))
    return specs
