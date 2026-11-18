# Vito

**An open-source, rule-enforced system for growing a small pocket of capital ($50–$200+) on [Binance](https://www.binance.com) (with legacy [OKX](https://www.okx.com) support).**

Everyone needs to grow their pocket somehow. The problem is that when you only have $90 or $200 to work with, the financial world gives you two bad choices:

1. **Leave it sitting in cash**, where inflation quietly eats it, or
2. **Gamble it on 5-minute charts, meme coins, or leveraged indicator bots** (like 1-hour MACD crossovers or "10x signals") that get chopped to pieces in sideways markets and donate your entire balance to the exchange in fees and liquidations.

**Vito is the third option.** It treats a small account like a disciplined quantitative treasury instead of a casino ticket. You can clone it, inspect every line of math, adjust the risk dial to match your own stomach, and use it as you see fit.

> ⚠️ **Plain-English disclaimer:** This is open-source research and execution software, not financial advice or a money-printing machine. Markets can go down, code can have bugs, and past backtests never guarantee future profits. Never put money into this system that you cannot afford to lose.

---

## What Are We Doing, and Why Does It Work? (In Layman's Terms)

Instead of trying to predict the next 15-minute candle on Bitcoin, Vito runs **two simple engines that work together** on a single Binance account:

### Engine 1: Ride the Strongest Macro Wave Across Crypto, Gold, Silver, and US Tech Stocks (`vito panel`)

At any given time in the world, *something* is usually in a real uptrend:
- Sometimes Crypto (**Bitcoin `BTC`**, **Ethereum `ETH`**, **Solana `SOL`**) is booming.
- Sometimes Crypto is crashing, and money is flowing into **Gold (`PAXG`)** or **Silver (`XAG`)**.
- Sometimes **US Tech & Big Companies (`QQQ` Nasdaq-100 / `SPY` S&P 500)** are steadily climbing while everything else chops sideways.

Because Binance now lists **Spot Crypto**, **Tokenized Gold (`PAXG-USDT`)**, and **24/7 TradFi Perpetuals (`QQQ-USDT-SWAP`, `SPY-USDT-SWAP`, `XAG-USDT-SWAP`)** with **$5 to $7 minimum order sizes**, even a **$90–$200 account** can rotate across all three asset classes.

Every **5 days** (on the daily close), Vito checks all 7 markets and asks two questions:
1. **Is this asset actually going up?** (Its 20-day return must be positive, and its price must be above its 20-day moving average. We never try to "catch a falling knife.")
2. **Which 2 of the rising assets have the cleanest, smoothest momentum?** (Ranked by 20-day return divided by volatility.)

Vito holds **only the top 2 winners**. Every position gets an automatic **trailing stop-loss** (`2.5 × ATR`, checked every day against intraday lows) so if a trend suddenly reverses or crashes, Vito cuts the position before a small loss turns into a disaster.

### Engine 2: Get Paid ~8%–10.25% APR While Waiting in Cash (`Simple Earn Flexible`)

What happens when Crypto, Stocks, *and* Metals are all falling at the same time?
**Nothing is bought.** When every market is in a downtrend, the smartest trade is **cash**.

Instead of leaving that cash earning 0%, Vito keeps uninvested USDT in **Binance Simple Earn Flexible**, which pays **~8% to 10.25% APR** (bonus tier on the first 1,000 USDT + real-time APR), credited daily with instant redemption the moment a new uptrend starts.

---

## Why We Don't Do "Leveraged MACD Long/Short" (We Tested It So You Don't Have To)

A very natural question is: *"Why sit in cash during downtrends? Why not trade 1-hour or 4-hour Futures long AND short using MACD crossovers and 3x–5x leverage?"*

We tested that exact idea on 2 years of real Binance `SOL-USDT-SWAP` candles starting with **$93.16 USDT**, paying Binance's actual `0.05%` taker fee + `0.02%` slippage:

| Strategy & Timeframe | Leverage | Starting Pocket | Ending Balance | Max Drawdown | Trades | Paid to Binance in Fees |
|---|---:|---:|---:|---:|---:|---:|
| `1h` Raw MACD Long/Short | `1x` | $93.16 | **$14.44** (−84.5%) | −88.5% | 1,313 | **$98.21** |
| `1h` MACD + 200-EMA + ATR Stop | `1x` | $93.16 | **$38.25** (−58.9%) | −69.9% | 666 | **$60.92** |
| `1h` MACD (Raw or Filtered) | `3x` / `5x` | $93.16 | **$0.00 (−100%)** | −100.0% | 95–515 | $19–$66 |
| `4h` MACD + 200-EMA + ATR Stop | `3x` | $93.16 | **$35.92** (−61.4%) | −86.4% | 160 | $30.20 |
| `4h` MACD (Raw or Filtered) | `5x` | $93.16 | **$0.00 (−100%)** | −100.0% | 27–103 | $6–$17 |
| `1d` MACD (Raw or Filtered) | `5x` | $93.16 | **$0.00 (−100%)** | −100.0% | 9–12 | $3.91–$7.29 |

**Why it fails every time:**
1. **Indicators like MACD lag.** By the time moving averages cross on a 1h or 4h chart, the move is already halfway over. In sideways chop, you buy the top and short the bottom repeatedly.
2. **The exchange eats your pocket in fees.** On 1-hour MACD, you pay **$98.21 in fees on a $93.16 account** — the house wins 100% of your money even at `1x` leverage.
3. **Bear-market short squeezes:** Even when we tested shorting downtrends on our daily panel, sudden 1-day bear-market bounces (+15% to +25%) knocked out trailing stops and lost **−$8.78**, whereas sitting in **Simple Earn at 10.25% APR** made money with zero drawdown.

---

## Why Hybrid Spot + TradFi Perps Is Built for Small Accounts ($50–$200)

On Binance USDⓈ-M Futures, the Bitcoin perpetual (`BTC-USDT-SWAP`) has a minimum order of `0.001 BTC` (**~$65 USDT**), which is too big to size properly on a $90–$200 account, and perpetual longs pay **5%–11% APR in funding fees** just to hold a position.

Because our strategy is **long-only**, Vito uses a **Hybrid Basket**:
- **Binance Spot (`$5.00` minimum order, `0%` funding fees, zero liquidation risk):** `BTC-USDT`, `ETH-USDT`, `SOL-USDT`, `PAXG-USDT` (Gold).
- **Binance TradFi Perps (`$5.00–$7.35` minimum order, 24/7 trading):** `QQQ-USDT-SWAP`, `SPY-USDT-SWAP`, `XAG-USDT-SWAP` (Silver).
- When the `1.50x` gross exposure dial requires more than your Spot cash across two winners, Leg 1 buys on **Spot** and Leg 2 automatically routes to its **USDⓈ-M Futures perpetual (`-SWAP`)** backed by the remaining USDT margin.

---

## Real Results & Choosing Your Own Risk Dial

In Vito, you never crank a "leverage knob" blindly. You choose **how much of your account you are willing to lose if a trade hits its stop-loss (`--risk-pct`)** and the **maximum total portfolio size (`--max-exposure`)**:

$$\text{Position Size} = \frac{\text{Account Equity} \times \text{Risk \%}}{\text{Stop Distance}}$$

Here is how the exact same 7-asset Hybrid Basket + Simple Earn (`8% APR` conservative cash yield) performed over **4 years of real Binance data (`2022-12-10 → 2026-11-18`)** starting with **$93.16 USDT**:

| Profile | Risk / Trade (`--risk-pct`) | Max Exposure (`--max-exposure`) | $93.16 Grows To | Total Return (CAGR) | Worst Drawdown | Sharpe (`t`-stat) |
|---|---:|---:|---:|---:|---:|---:|
| **Conservative** | `1.5%` | `0.75x` | **$200.44** | **+115.1%** (+21.4%/yr) | **−10.2%** | 1.89 (`+3.74`) |
| **Balanced (Full Spot)** | `2.0%` | `1.00x` | **$230.53** | **+147.4%** (+25.8%/yr) | **−14.5%** | 1.70 (`+3.37`) |
| **Aggressive** | `2.5%` | `1.25x` | **$264.68** | **+184.1%** (+30.3%/yr) | **−18.7%** | 1.59 (`+3.16`) |
| **Full Throttle (Default)** | `3.0%` | `1.50x` | **$303.64** | **+225.9%** (+34.9%/yr) | **−22.6%** | 1.52 (`+3.02`) |

*(For comparison, buying and holding all 7 assets equally suffered a **−44.0% drawdown**, and during the Apr–Nov 2026 crypto slide where the equal-weight basket lost **−13.53%**, Vito made **+7.12% with an −8.3% max drawdown** by rotating into `QQQ`, `XAG`, and `SPY`.)*

---

## Quick Start (Install & Run in 2 Minutes)

Requires **Python ≥ 3.11**. Works out of the box with **no API keys** using public Binance market data.

```bash
git clone https://github.com/triloger/vito
cd vito

python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

### 1. Run the 4-Year Cross-Asset Momentum Backtest
```bash
# Default dial: 3.0% risk/trade, 1.50x max exposure, 8% Simple Earn cash yield
.venv/bin/vito panel --initial 100 --days 1460

# Or try the Conservative dial (1.5% risk, 0.75x exposure):
.venv/bin/vito panel --initial 100 --days 1460 --risk-pct 1.5 --max-exposure 0.75
```

### 2. Connect Your Binance Account (Read-Only First)
See [`docs/binance-setup.md`](docs/binance-setup.md) for the full guide. Create a read-only API key on Binance (with **Withdrawals OFF**), then run:

```bash
.venv/bin/vito setup
```
It prompts for your API Key and Secret with hidden input (`getpass`), saves `.env` with `0600` permissions, and verifies your Spot + Futures + Simple Earn balance.

### 3. Daily Commands
```bash
.venv/bin/vito status        # Live market prices, funding APRs, your total USDT (incl. Simple Earn), and active guards
.venv/bin/vito shadow        # Live Shadow Execution Plan: checks regime, Simple Earn APR, and exact intended orders
.venv/bin/vito paper panel   # Step the live paper portfolio forward on newly closed daily candles
.venv/bin/vito paper status  # View paper trading scoreboard
.venv/bin/vito instruments   # Scan all Binance Spot + Futures markets that fit a small account
.venv/bin/vito doctor        # Verify Python, ccxt, policy digest, Binance API, egress IP, and audit log
```

---

## Built-In Safety Rails (The Constitution)

Loaded from [`config/policy.toml`](config/policy.toml) and enforced in code:

1. **Hard Equity Floor (`$50.00 USDT`):** If account equity ever drops below `$50.00`, Vito refuses to open new positions.
2. **Intrabar Trailing ATR Stops:** Every position has a `2.5 × ATR_14` stop that ratchets up as price rises and never moves down.
3. **Emergency Kill Switch:** Run `touch ~/.vito/HALT` at any time and every paper, shadow, and live router halts immediately.
4. **Tamper-Evident Audit Chain (`~/.vito/audit.jsonl`):** Every shadow plan, order, and refusal is SHA-256 hash-chained to the previous entry.
5. **Four-Key Execution Lock:** Live order routing (`OrderRouter.arm`) is physically locked unless `execution.mode = "live"`, `VITO_LIVE=1` is set, the exact 16-character policy digest is confirmed, and no `HALT` file exists.

---

## Repository Layout

```text
src/vito/
  policy.py          risk constitution: load, validate, SHA-256 digest
  risk/              position sizing, liquidation math, circuit breakers
  exchange/          Binance gateway (Spot + USDⓈ-M Futures + Simple Earn) & legacy OKX gateway
  data/              candles, funding history, and small-account instrument scanner
  portfolio/         unified account valuation across Spot, Futures, Funding, and Simple Earn
  strategy/          indicators (ATR, EMA, RSI, Donchian) & single-series strategies
  backtest/          single-asset engine, walk-forward validator, and cross-asset momentum panel
  paper/             step-driven single-asset & cross-asset paper brokers
  exec/              shadow planner, locked OrderRouter, and hash-chained AuditLog
  cli.py             status | setup | doctor | net | policy | instruments | backtest | walkforward | panel | shadow | paper
docs/                binance-setup, okx-setup, architecture, risk-policy, paper, research journal
config/              policy.toml, config.example.toml
tests/               136 unit tests, zero network calls required
```

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, test your own baskets, and grow your pocket responsibly.
