from __future__ import annotations

import pytest

from vito.data.instruments import classify_asset, evaluate_market


def test_classify_asset_uses_okx_inst_category() -> None:
    assert classify_asset({"base": "BTC", "info": {"instCategory": "1"}}) == "crypto"
    assert classify_asset({"base": "NVDA", "info": {"instCategory": "3"}}) == "equity"
    assert classify_asset({"base": "XAU", "info": {"instCategory": "4"}}) == "commodity"


def test_classify_asset_uses_binance_tradfi_metadata() -> None:
    assert (
        classify_asset(
            {
                "base": "QQQ",
                "info": {
                    "contractType": "TRADIFI_PERPETUAL",
                    "underlyingType": "EQUITY",
                    "underlyingSubType": ["TradFi"],
                },
            }
        )
        == "equity"
    )
    assert (
        classify_asset(
            {
                "base": "XAU",
                "info": {
                    "contractType": "TRADIFI_PERPETUAL",
                    "underlyingType": "COMMODITY",
                },
            }
        )
        == "commodity"
    )
    assert (
        classify_asset(
            {
                "base": "BTC",
                "info": {"contractType": "PERPETUAL", "underlyingType": "COIN"},
            }
        )
        == "crypto"
    )


def test_classify_asset_fallback_distinguishes_crypto_x_tokens() -> None:
    assert classify_asset({"base": "XRP", "info": {}}) == "crypto"
    assert classify_asset({"base": "XLM", "info": {}}) == "crypto"
    assert classify_asset({"base": "XAUT", "info": {}}) == "commodity"
    assert classify_asset({"base": "XNVDA", "info": {}}) == "equity"


def test_evaluate_swap_uses_contract_size_and_base_vol_ccy() -> None:
    market = {
        "id": "BTC-USDT-SWAP",
        "symbol": "BTC/USDT:USDT",
        "type": "swap",
        "linear": True,
        "quote": "USDT",
        "base": "BTC",
        "active": True,
        "contractSize": 0.01,
        "limits": {"amount": {"min": 0.01}},
        "precision": {"price": 0.1},
        "maker": 0.0002,
        "taker": 0.0005,
        "info": {"instCategory": "1", "lever": "100", "listTime": "1573557408000"},
    }
    ticker = {
        "last": 65000.0,
        "bid": 64999.9,
        "ask": 65000.1,
        "info": {"volCcy24h": "1000"},  # 1000 BTC
    }
    spec = evaluate_market(market, ticker, max_order_usdt=150.0)
    assert spec is not None
    assert spec.asset_class == "crypto"
    # 0.01 ctVal * 0.01 minSz * 65000 = $6.50
    assert spec.min_notional_usdt == pytest.approx(6.50)
    assert spec.volume_24h_usdt == pytest.approx(65_000_000.0)
    assert spec.tradeable is True
    assert spec.reject_reason == ""


def test_evaluate_market_rejects_illiquid_and_wide_spread() -> None:
    market = {
        "id": "XILLIQ-USDT",
        "symbol": "XILLIQ/USDT",
        "type": "spot",
        "quote": "USDT",
        "base": "XILLIQ",
        "active": True,
        "contractSize": 1.0,
        "limits": {"amount": {"min": 1.0}},
        "precision": {"price": 0.01},
        "maker": 0.001,
        "taker": 0.0015,
        "info": {"instCategory": "3", "lever": "10"},
    }
    ticker = {
        "last": 200.0,
        "bid": 198.0,
        "ask": 202.0,
        "info": {"volCcy24h": "500"},
    }
    spec = evaluate_market(
        market,
        ticker,
        max_order_usdt=150.0,
        min_volume_usdt=100_000.0,
        max_spread_bps=10.0,
    )
    assert spec is not None
    assert spec.tradeable is False
    assert "min_notional>150" in spec.reject_reason
    assert "illiquid" in spec.reject_reason
    assert "wide_spread" in spec.reject_reason


def test_evaluate_binance_swap_respects_min_cost_floor() -> None:
    market = {
        "id": "SOLUSDT",
        "symbol": "SOL/USDT:USDT",
        "type": "swap",
        "linear": True,
        "quote": "USDT",
        "base": "SOL",
        "active": True,
        "contractSize": 1.0,
        "limits": {"amount": {"min": 0.01}, "cost": {"min": 5.0}},
        "precision": {"price": 0.01},
        "maker": 0.0002,
        "taker": 0.0005,
        "info": {"contractType": "PERPETUAL", "underlyingType": "COIN"},
    }
    ticker = {"last": 80.0, "bid": 79.99, "ask": 80.01, "quoteVolume": 50_000_000.0}
    spec = evaluate_market(market, ticker, max_order_usdt=150.0)
    assert spec is not None
    assert spec.inst_id == "SOL-USDT-SWAP"
    # 0.01 * 80 = $0.80, but min_cost is $5.00 -> effective min_notional is $5.00
    assert spec.min_notional_usdt == pytest.approx(5.0)
    assert spec.tradeable is True
