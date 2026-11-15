# Architecture

## The one rule

A component either **cannot** touch money or it is **locked**. There is no
third category, and nothing is allowed to grow into one by accident.

```
                 ┌──────────────────────────────────────────┐
   public data   │  exchange/okx.py                         │
  ──────────────▶│  one ccxt wrapper, credential-aware      │
                 └───────────────┬──────────────────────────┘
                                 │
        ┌────────────────────────┼─────────────────────────┐
        ▼                        ▼                         ▼
┌───────────────┐      ┌──────────────────┐      ┌──────────────────┐
│ data/market   │      │ portfolio/       │      │ exec/            │
│ price,funding │      │ snapshot, value  │      │ OrderRouter      │
└───────┬───────┘      └──────────────────┘      │ + AuditLog       │
        │                                        └────────┬─────────┘
        ▼                                                 │
┌───────────────┐      ┌──────────────────┐               │
│ strategy/     │─────▶│ risk/limits.py   │───────────────┘
│ Signal        │      │ size_position()  │  only sized, approved
└───────┬───────┘      └────────┬─────────┘  orders get here
        │                       │
        ▼                       ▼
┌───────────────┐      ┌──────────────────┐
│ backtest/     │      │ policy.toml      │
│ paper/        │      │ the constitution │
└───────────────┘      └──────────────────┘
```

Data flows one way: market data → signal → **policy** → sized order → router.
A signal never reaches the router directly, and the router never trusts a
caller to have checked the policy.

## Modules

| Module | Responsibility | Side effects |
| --- | --- | --- |
| `policy.py` | Load, validate and digest `config/policy.toml` | reads one file |
| `risk/limits.py` | Sizing, liquidation maths, circuit breakers | **none** (pure) |
| `exchange/okx.py` | OKX via ccxt; refuses private calls without keys | network |
| `data/market.py` | Ticker + funding rows, degrades per-symbol | network (via gateway) |
| `portfolio/snapshot.py` | Value holdings in USDT; flags unpriced assets | network (via gateway) |
| `strategy/base.py` | `Signal` type and `Strategy` protocol | **none** (pure) |
| `backtest/`, `paper/` | Phase 2 | not implemented |
| `exec/` | `OrderRouter` (locked), `AuditLog` (hash-chained) | filesystem, later network |
| `cli.py` | Presentation only | stdout |

Two modules are pure — `risk` and `strategy` — and that is deliberate. Pure
modules can be tested exhaustively without a network, a clock, or a funded
account. Everything that moves money depends on `risk`, so `risk` gets the
highest test coverage in the repository.

## Why the policy is a file, not code

Risk parameters change (equity changes, volatility regimes change, the operator
learns something). If they lived in Python, every change would be a code review
and a deploy, and they would drift through ad-hoc edits.

Making them a TOML file with a **content digest** gives the best of both:

* changing a cap is a one-line edit, easy to review in a diff;
* the digest changes, which revokes any armed executor;
* the values are validated on load — an unsafe policy refuses to start.

`execution.allow_live_from_config` exists to be `false`. It makes "arm me from a
config file" impossible by construction: a stray edit cannot arm the executor,
because the field itself is rejected if it is ever true.

## Execution lock

```python
router = OrderRouter(policy)
router.place(order)          # ExecutionLocked: not armed
router.arm(policy.digest())  # ExecutionLocked: mode is readonly
```

Arming requires four independent conditions (mode, `VITO_LIVE`, digest, no HALT
file). They are checked in `OrderRouter.arm`, and `place` re-checks the armed
flag on every call. Defence in depth is cheap here; the failure mode is not.

## Audit log

Every decision — including refusals, which are the interesting ones — appends a
JSON line to `~/.vito/audit.jsonl`. Each record embeds the previous record's
hash, so the chain can be replayed and verified. A silently edited history fails
`AuditLog.verify()`.

## State

Vito is **stateless between runs** in Phase 1: it reads the exchange, prints, and
exits. Persistent state (equity peak, daily PnL, loss streak, open positions)
lands in Phase 3 together with the audit log's first real entries. Until then the
circuit-breaker functions exist and are tested, but nothing feeds them.
