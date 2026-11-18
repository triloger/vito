# Connecting Binance

Vito works with **no API key** for research, backtesting (`vito panel`, `vito backtest`), and paper trading (`vito paper panel`). You only need an API key when you want Vito to see your real balance, track your **Simple Earn Flexible** yield, and run daily shadow/live execution plans (`vito status`, `vito shadow`).

---

## 1. Why Binance (and the regional reality)

In many countries outside the US/EU (such as the Dominican Republic and across Latin America), exchanges like OKX restrict API trading to institutional accounts. **Binance (`binance.com`)** allows retail API access AND lists both **Spot Crypto/Gold** (`BTC`, `ETH`, `SOL`, `PAXG`) and **24/7 TradFi Perpetuals** (`QQQ`, `SPY`, `XAU`, `XAG`) under a single roof with **$5.00–$7.35 minimum orders**.

> **Important if you host on a cloud VM:** `api.binance.com` and `fapi.binance.com` block US IP addresses with `HTTP 451`. If your cloud server is in `us-central1` or `us-east1`, move it to a non-US region such as **São Paulo (`southamerica-east1`)** or run from your local non-US connection.

---

## 2. Keep idle USDT in Simple Earn Flexible (`Auto-Subscribe`)

Before or after funding your account, turn on **Simple Earn Flexible (`USDT`)** in Binance:
- Up to 1,000 USDT typically earns **~8%–10.25% APR** (bonus tier + real-time APR), paid daily.
- It is redeemable instantly (`canRedeem: true`).
- Note: Binance's legacy Spot API reports Simple Earn shares as `LDUSDT` (at an internal pool exchange rate > 1.0, so $93.16 of USDT looks like ~81.53 `LDUSDT` shares). Vito automatically ignores the raw `LD*` share count and queries `/sapi/v1/simple-earn/flexible/position` so `vito status` always shows your true USDT value and live APR.

---

## 3. Create the API Key safely

In Binance: **Profile → API Management → Create API** (System-generated):

| Setting | Value | Why |
|---|---|---|
| **Enable Reading** | **ON** | Needed for `vito status` and `vito shadow` |
| **Enable Spot & Margin / Futures** | **OFF** until live promotion | Keep the key physically incapable of trading while you test |
| **Enable Withdrawals** | **OFF, always** | Vito has no withdrawal code and never needs this permission |
| **IP access restrictions** | **Restrict access to trusted IPs only** | Paste the output of `vito net` from your host |

---

## 4. Store the key with `vito setup` (hidden input, mode `0600`)

Run on your machine or VM:

```bash
vito setup
```

It prompts for your **Binance API Key** and **Binance API Secret** with terminal echo turned off (nothing appears on screen or in your shell history), writes `.env` with `0600` (owner-only) permissions, and immediately verifies your balance with Binance.

Then verify:

```bash
vito doctor
vito status
vito shadow
```
