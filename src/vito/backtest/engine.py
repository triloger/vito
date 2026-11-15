"""Event-driven backtesting (Phase 2).

Planned interface::

    result = run_backtest(strategy, candles, policy, fees=..., funding=...)

The engine must model, at minimum:

* taker/maker fees on both legs,
* slippage up to ``policy.risk.max_slippage_pct``,
* funding payments for perp positions,
* intrabar stop and take-profit fills (with gap-through handling),
* the same ``vito.risk.size_position`` used live, so results are comparable.

Until that exists, this module refuses to guess.
"""

from __future__ import annotations


class BacktestNotReady(NotImplementedError):
    """Phase 2 has not landed yet."""


def run_backtest(*args, **kwargs):  # noqa: ANN002, ANN003
    raise BacktestNotReady(
        "The backtester lands in Phase 2. Run `vito status` for the read-only "
        "dashboard; do not paper-trade a strategy that has not been backtested."
    )
