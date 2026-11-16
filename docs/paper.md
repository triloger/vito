# Paper trading

Paper mode runs the **same sizing and fill rules as the backtester** against live
exchange candles, with no capital and no API key. It exists to prove the plumbing
— that a signal becomes a sized position, that a stop is honoured, that state
survives a reboot — before any money is exposed.

It is not a return estimate. Read the "does not model" section before drawing any
conclusion from paper PnL.

## How it works

`vito paper step` is a single, idempotent evaluation:

1. Fetch the most recent candles for the instrument.
2. Drop the still-forming candle. Trading a bar that has not closed is the live
   version of lookahead.
3. Compare the newest closed bar against the watermark in the state file.
4. Apply every bar after the watermark, in order, using the engine's rules:
   decide on the close, fill at the next open, check the stop intrabar, fill at
   the open if the bar gapped through it.
5. Write state back atomically.

Running it twice in the same minute does nothing the second time. If the timer
was down for three days, the next run applies three days of bars in order.

State lives in `~/.vito/paper-<instrument>_<strategy>_<timeframe>.json` and holds
equity, open position, the full trade log, refused entries, and the watermark.
It is plain JSON so you can read it without the tool.

## Usage

```bash
# first run warms up on history and deliberately does not enter
vito paper step --symbol BTC-USDT-SWAP --timeframe 1d --strategy breakout

# subsequent runs are no-ops until a new bar closes
vito paper step --symbol BTC-USDT-SWAP --timeframe 1d --strategy breakout

vito paper status
vito paper status --state ~/.vito/paper-BTC-USDT-SWAP_breakout_1d.json --json
vito paper step --reset          # discard the run and start over
```

Parameters use `--param key=value`:

```bash
vito paper step --strategy breakout --param window=30 --param atr_mult=2.5
```

## Scheduling it

A daily bar needs one check shortly after the close; a four-hour bar needs one
every four hours. A systemd timer is the least surprising option.

`/etc/systemd/system/vito-paper.service`:

```ini
[Unit]
Description=Vito paper step
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=vito
WorkingDirectory=/home/vito/vito
ExecStart=/home/vito/vito/.venv/bin/python -m vito paper step \
    --symbol BTC-USDT-SWAP --timeframe 1d --strategy breakout
```

`/etc/systemd/system/vito-paper.timer`:

```ini
[Unit]
Description=Run Vito paper step shortly after each daily close

[Timer]
OnCalendar=*-*-* 00:10:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now vito-paper.timer
systemctl list-timers vito-paper.timer
journalctl -u vito-paper.service -n 50
```

`Persistent=true` is what makes a missed run catch up after a reboot. Without it
a VM restart silently skips bars.

## What paper does not model

| Missing | Consequence |
| --- | --- |
| Funding payments | A long held through positive funding looks better than it is |
| Spread widening | Entries and exits are too cheap |
| Partial fills, rejections | The strategy always gets the size it asks for |
| Latency between close and fill | Fills are optimistically timed |

These are exactly what **shadow mode** measures later: run the decisions, place
nothing, and diff intended fills against the real book. Until then, treat paper
PnL as a plumbing signal, not evidence of edge.

## The one invariant that matters

Over the same bars, paper and `backtest.engine` must produce identical trades.
`tests/test_paper.py::test_paper_matches_backtest` asserts it round for round —
same entry timestamp, same fill price, same fees, same exit reason. Two
implementations of one rule set are allowed to be slow; they are not allowed to
disagree.

## Promotion

Paper does not unlock live trading. The gate is:

1. ✅ backtest — **failed** (no out-of-sample edge; see the journal)
2. ⬜ paper — ≥200 trades or 30 days, and the state file has to be boring
3. ⬜ shadow — 7 days of intended-vs-actual fills
4. ⬜ live — only with an explicit `--i-understand` flag and the policy digest

A step can be green while the strategy is bad. Those are separate questions, and
the second one is currently unanswered.
