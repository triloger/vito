# Risk policy

The policy lives in [`config/policy.toml`](../config/policy.toml) and is enforced
by [`src/vito/risk/limits.py`](../src/vito/risk/limits.py). This document explains
*why* each number is what it is.

## The governing equation

```
notional = (equity × risk_pct) / stop_distance
```

Everything follows from this. You choose how much you are willing to lose if the
stop is hit (`risk_pct`), and where the stop goes (`stop_distance`). Those two
numbers decide position size. Leverage does not appear in the equation at all.

At $200 equity, 1.5% risk, and a 1% stop:

```
notional = (200 × 0.015) / 0.01 = $300  →  1.5× equity
```

That is the whole trick. **Leverage is a ceiling applied afterwards, not a
multiplier applied before.** A trader who thinks in leverage sizes positions by
"how much can I borrow"; a trader who thinks in risk sizes positions by "how much
can I lose". The second one survives.

### Why the leverage cap is 6×

At 6× the liquidation price sits roughly

```
1/6 − 0.004 = 16.3%
```

away from entry. Against a 0.6% minimum stop that is a 27× buffer: the stop fires
long before the exchange does. The cap is therefore a guardrail, not a governor.
Raising it from 3× to 6× changed nothing about position sizes; it only removed an
artificial constraint for wide-stop, high-conviction setups. That is why the
"make it more aggressive" dial that actually matters is `risk_per_trade_pct`.

### Why gross exposure is capped at 3× — and why that one does not move

Gross exposure is **gap risk**, and gap risk is the only risk a stop cannot
protect you from. At 3× equity, a 33% adverse move on the whole book is
unrecoverable. At 1.5× it takes 66%. BTC has printed 30% days; correlated
alt-coins have printed worse.

This is also why the instrument whitelist exists. At $200, one wick in a
low-liquidity perpetual is not a drawdown, it is a liquidation.

## Liquidation maths

Two functions in `risk/limits.py`, both unit-tested:

```python
liquidation_distance(leverage, mmr)        # ≈ 1/L − mmr
max_leverage_for_stop(stop, k, mmr)        # L ≤ 1/(k·stop + mmr)
```

The second is the buffer rule: liquidation must sit at least `k` stop-widths
away. Solving `1/L − mmr ≥ k·stop` for `L` gives the maximum leverage that
satisfies it. With `k = 5`, a 1% stop allows up to ~18.5× — so the buffer rule
only bites in pathological configurations, which is exactly what a safety net
should do.

`mmr = 0.004` is the tier-1 maintenance margin rate for BTC/ETH swaps on OKX. The
exchange's real formula also includes fees and tiered margins, so treat the
computed distance as a **conservative estimate**, not gospel.

## The numbers

| Parameter | Value | Reasoning |
| --- | --- | --- |
| `risk_per_trade_pct` | 1.5% | 20 consecutive losses ≈ −26%, recoverable. 3% would be −46%. |
| `conviction_risk_pct` | 2.0% | One notch up, BTC only — the deepest book and the tightest spread. |
| `max_leverage` | 6× | 16% away from liquidation; never binds under normal sizing. |
| `max_gross_exposure_mult` | 3× | 33% gap to ruin. The real constraint. |
| `max_open_positions` | 2 | Correlation is not diversification when everything trades on BTC beta. |
| `min_stop_distance_pct` | 0.6% | Below this, the stop is inside the noise, not outside the move. |
| `liquidation_buffer_mult` | 5× | The stop must win every race against the exchange. |
| `max_slippage_pct` | 0.15% | On a $300 round trip, 0.15% is $0.45 — already 15% of the risk budget. |
| `funding_guard_minutes` | 15 | Do not pay rent on entry because you arrived 30 seconds early. |
| `daily_loss_halt_pct` | 3% | Two full-size losses in a day means the regime is not cooperating. |
| `peak_drawdown_stop_pct` | 15% | Manual restart. This is the line that turns "bad month" into "stop". |
| `loss_streak_cooldown` | 4 → 24h | Four losses is a pattern; a 24h pause is cheaper than a fifth. |

## The arithmetic of small accounts

$200 is not a small version of $20,000. Fixed costs dominate:

* **Fees.** OKX taker on swaps is ~0.05% per side. A $300 round trip pays
  ~$0.30, which is **10% of a $3 risk budget**. High-frequency strategies are
  structurally unviable here, regardless of edge.
* **Minimum size.** BTC-USDT-SWAP minimum is 0.01 contracts = 0.0001 BTC ≈ $6.50,
  ETH ≈ $1.83. Granularity is fine; fees are the binding constraint.
* **Funding.** At 1.79% APR annualised (a typical quiet-period rate), a $300
  position pays ~$0.015/day. Negligible — until the rate spikes to 50% APR during
  a squeeze, when it becomes the dominant cost.

The practical consequence: **fewer, larger, longer-held trades.** That is why the
minimum stop distance exists, and why the conviction tier is restricted to BTC.

## What "success" looks like

A realistic excellent outcome for a discretionary/quant hybrid at this size is
**+2–5% per month** with a maximum drawdown under 10%. Anyone quoting more is
describing variance or selling something.

The policy's job is not to maximise return. It is to make the bad outcome
*recoverable* rather than *terminal*, so that many months of small edges can
compound instead of one tail event ending the account.

## Changing the policy

1. Edit `config/policy.toml`.
2. Run `vito policy` — the digest changes, revoking any armed executor.
3. Run `pytest` — the risk tests assert the relationships between caps, not just
   their values.
4. Record the change and the reason in `docs/journal/`.

Loosening a cap during a drawdown is the single most reliable way to turn a
manageable loss into a total one. If you feel the urge, `touch ~/.vito/HALT`
first and wait a day.
