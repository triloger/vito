# Connecting OKX

Vito needs **no** API key to run. This page is for when you want account data,
and later, execution.

Read the whole page before creating a key. The defaults below are the ones that
keep a mistake survivable.

## 1. Create a dedicated sub-account

In OKX: **Assets → Sub-accounts → Create sub-account**.

Transfer **only your risk capital** into it. The sub-account is the blast radius:
if the key leaks, if a strategy misbehaves, if Vito has a bug, that balance is the
maximum that can be lost. It also keeps your main account's history and API keys
out of reach.

## 2. Create an API key on that sub-account

**Profile → API → Create API key** on the sub-account.

| Setting | Value | Why |
| --- | --- | --- |
| Permissions | **Read** to start | Vito is read-only until you promote it. |
| | Read + Trade later | Trade is needed only for Phase 3 execution. |
| Withdraw | **OFF, always** | Nothing in this repository can withdraw. Do not give it the ability. |
| Passphrase | strong, unique | It is a third credential; treat it like the secret. |
| IP allowlist | **ON** | The single most effective control you have. |

> **If you cannot use an IP allowlist** (dynamic IP), use a VPS with a static IP
> and run Vito there. An unrestricted key is one credential-stuffing attempt away
> from a drained account.

Save the three values when they are shown — the secret is displayed once.

## 3. Store the credentials

```bash
cp .env.example .env
$EDITOR .env
```

```dotenv
VITO_OKX_API_KEY=...
VITO_OKX_API_SECRET=...
VITO_OKX_API_PASSPHRASE=...
VITO_OKX_DEMO=0
VITO_MODE=readonly
VITO_LIVE=0
```

`.env` is gitignored. Confirm before your first push:

```bash
git status --porcelain | grep -q '\.env$' && echo "STOP: .env is tracked"
```

## 4. Verify

```bash
vito doctor
```

Expected:

```
✓ python        3.12.3 (need >= 3.11)
✓ ccxt          4.5.92
✓ policy        v1.1 · a2cc053d5c999834
✓ credentials   abcd…efgh
✓ okx reachable api/v5
✓ kill switch   clear
✓ audit log     /home/you/.vito
```

Then:

```bash
vito status
```

The **Account** panel should show your holdings valued in USDT, with a cash
percentage. An `unpriced` tag means Vito could not find a spot pair for that
currency — the total is then understated rather than silently wrong.

## 5. Demo trading (recommended before live)

OKX offers demo trading with separate keys. Set `VITO_OKX_DEMO=1` and use the
demo key. Vito routes to the sandbox automatically. Demo fills are unrealistically
good — use it to validate plumbing, not to validate a strategy.

## 6. Promoting to live execution

There is no button for this, by design. In order:

1. `execution.mode = "paper"` in `config/policy.toml` — run Phase 2 until you
   have a sample.
2. Read [`risk-policy.md`](risk-policy.md) again, specifically the drawdown math.
3. Set `execution.mode = "live"`, add **Trade** permission to the key, and keep
   withdraw off.
4. `export VITO_LIVE=1` and arm with the digest printed by `vito policy`.

Any edit to `policy.toml` changes the digest and unarms the executor. That is the
feature.

## Revoking access

If anything looks wrong:

```bash
touch ~/.vito/HALT          # stops Vito immediately
```

Then delete the API key in OKX. Order of operations matters: halt first, revoke
second. Removing the key while a router is armed leaves it retrying with stale
credentials.

## What Vito will never ask for

* your OKX login password,
* your 2FA code,
* withdraw permission,
* your seed phrase or private keys.

Anything claiming to be Vito and asking for those is not Vito.
