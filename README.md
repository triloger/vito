# Vito

A personal quantitative treasury for a single [OKX](https://www.okx.com) account.

Vito is not a trading bot you point at your savings. It is a small, opinionated
system built around one idea: **the risk policy is the product, and everything
else exists to enforce it.** Strategies, backtests and order routing are all
subordinate to a set of caps that are checked in code and refuse to negotiate.

> ⚠️ **Not financial advice.** This is personal research software. It ships with
> no promise of profit, no "edge", and no guarantee that it will not lose money.
> Crypto derivatives can lose more than the balance in the account. If you run
> it live, you are the one taking the risk — and you should assume the code has
> bugs, because it does.

---

## Design

Three layers, deliberately separated so that a mistake in one cannot reach the
money in another:

| Layer | Modules | Money at risk |
| --- | --- | --- |
| **Research** | `data`, `strategy`, `backtest`, `paper` | none |
| **Policy** | `policy`, `risk` | none — but it decides everything |
| **Execution** | `exec` | yes, and it is locked by default |

The execution layer is a *locked* object, not a switch. Arming it requires:

1. `execution.mode = "live"` in `config/policy.toml`,
2. `VITO_LIVE=1` in the environment,
3. typing the current **policy digest** (`vito policy` prints it),
4. no `HALT` file present.

Any edit to a risk parameter changes the digest, which invalidates arming. Risk
settings are not allowed to drift silently.

## The risk policy

Loaded from [`config/policy.toml`](config/policy.toml), validated on every run,
and unit-tested in [`tests/test_risk.py`](tests/test_risk.py).

| Control | v1.1 |
| --- | --- |
| Risk per trade | **1.5%** of equity at the stop |
| Conviction tier | **2.0%**, BTC-USDT-SWAP only |
| Max leverage per position | **6×** (a ceiling; usually non-binding) |
| Max gross exposure | **3.0×** equity (gap-risk limit) |
| Max open positions | **2** |
| Min stop distance | **0.6%** — tighter than that is noise |
| Liquidation buffer | liquidation must sit **≥5 stop-widths** away |
| Daily loss halt | **3%** → no new positions for 24h |
| Peak drawdown stop | **15%** → everything stops, manual reset |
| Loss-streak cooldown | **4** consecutive losses → 24h pause |
| Instruments | BTC/ETH swaps and spot only |

The key piece of arithmetic, which is why the leverage cap rarely binds:

```
notional = (equity × risk_pct) / stop_distance
```

At 1.5% risk on $200 with a 1% stop, that is a **$300 position — 1.5× equity**.
Leverage is a ceiling applied afterwards to stop pathological configurations.
Position size is governed by the stop and the risk budget, not by the leverage
dial. See [`docs/risk-policy.md`](docs/risk-policy.md) for the reasoning behind
each number and the liquidation maths.

## Install

Requires Python ≥ 3.11.

```bash
git clone https://github.com/triloger/vito
cd vito

python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

If `python3 -m venv` fails with *"ensurepip is not available"* on Debian/Ubuntu:

```bash
python3 -m venv --without-pip .venv
curl -sSL https://bootstrap.pypa.io/get-pip.py | .venv/bin/python
.venv/bin/python -m pip install -e ".[dev]"
```

## Usage

Vito works with **no API keys at all** — it falls back to public market data and
tells you how to connect an account.

```bash
vito status     # market context + account snapshot
vito policy     # the constitution and its digest
vito net        # egress IP and allowlist drift
vito doctor     # environment, connectivity, credentials
vito backtest   # run a strategy over real OKX candles
```

or without installing: `python -m vito status`.

`vito status` prints a market table (price, 24h change, spread, funding and its
annualised cost), the account holdings valued in USDT, and the active guards.

```bash
vito status --json    # machine-readable, for dashboards or cron
```

### Backtesting

```bash
vito backtest --symbol BTC-USDT-SWAP --timeframe 1d --days 730
vito backtest --symbol ETH-USDT-SWAP --timeframe 4h --strategy trend --json
```

The engine pays taker fees on both legs, slippage on both legs, and funding
every interval while a perpetual is open. Entries execute at the **next** bar's
open, stops are checked intrabar and fill at the open when a bar gaps through
them, and every entry is sized by the same `size_position` the live router uses —
so a backtest cannot be more aggressive than the executor.

Results over 2024-11 → 2026-11 (BTC, 200 USDT, 5 bps):

| Symbol | TF | Strategy | Return | Max DD | Sharpe | Trades | Fees |
| --- | --- | --- | --- | --- | --- | --- | --- |
| BTC | 1d | breakout | +8.5% | −5.0% | 0.90 | 44 | 2.49 |
| BTC | 1d | trend | +1.1% | −11.8% | 0.10 | 30 | 1.71 |
| BTC | 4h | trend | +0.5% | −21.9% | 0.14 | 222 | 27.10 |
| BTC | 4h | meanrev | −25.1% | −27.5% | −1.60 | 200 | 18.67 |
| BTC | 4h | buy & hold | −28.6% | −53.4% | −0.17 | — | — |

The headline is not a return. It is that **fees on 222 trades cost 27 USDT —
13.5% of a 200 USDT account — while 44 trades cost 2.49.** Frequency, not signal
quality, decided the outcome. Treat every number above as in-sample and
unproven; 44 trades is not a sample, it is an anecdote. See
[`docs/journal/`](docs/journal/).

### Connecting an account (read-only first)

Vito is read-only until you deliberately promote it. To see balances:

```bash
cp .env.example .env
$EDITOR .env            # API key, secret, passphrase
vito status
```

Create the key on a **dedicated sub-account** with **Read** permission, withdraw
disabled, and the IP allowlist enabled. Full walkthrough, including the
sub-account strategy and why the passphrase matters:
[`docs/okx-setup.md`](docs/okx-setup.md).

## Roadmap

| Phase | Deliverable | Gate to next phase |
| --- | --- | --- |
| **0** ✅ | Repo, policy, docs, CI | — |
| **1** ✅ | Read-only dashboard | `vito doctor` clean |
| **2** ✅ | Backtester (fees, funding, slippage) | beats buy-and-hold *in sample* — not yet out of sample |
| **2b** | Paper broker | ≥200 paper trades or 30 days positive |
| **3** | Guarded live routing | 7 days shadow-diffed against live |
| **4** | Daily report, journal, rebalance | — |
| **5** | Options, multi-instrument | Only if account equity justifies contract minimums |

Placeholders for Phase 3+ raise `NotImplementedError` with an explanation rather
than pretending to work. Nothing in this repository can place an order today.

## Safety rules (non-negotiable)

1. **Never** enable withdraw permission on the API key. Vito cannot withdraw,
   and it should not be able to.
2. **Never** run this on an account holding more than you can lose entirely.
3. **Never** widen a risk cap because of a losing streak. That is the exact
   behaviour the policy exists to prevent.
4. **Never** commit `.env`. It is gitignored; verify with `git status` before
   every push.
5. If in doubt: `touch ~/.vito/HALT`. Everything stops.

## Repository layout

```
src/vito/
  policy.py        risk constitution: load, validate, digest
  risk/            position sizing + circuit breakers (pure, tested)
  exchange/        OKX gateway (read-only by default)
  data/            market context: price, funding, spread, candles
  portfolio/       account snapshot and valuation
  strategy/        indicators + trend / breakout / meanrev (pure functions)
  network.py       egress IP + allowlist drift detection
  backtest/        event-driven engine: fees, funding, slippage, gap fills
  paper/           paper broker                   [phase 2b]
  exec/            order router + hash-chained audit log
  cli.py           vito status | policy | net | doctor | backtest
docs/              architecture, risk policy, OKX setup, journal
config/            policy.toml, config.example.toml
tests/             96 tests, no network required
```

## License

MIT — see [LICENSE](LICENSE).
