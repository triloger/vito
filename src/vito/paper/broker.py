"""Paper broker (Phase 2).

Simulates fills against live market data through the same order interface the
live router uses, so that promoting a strategy to live changes one object, not
the strategy code.
"""

from __future__ import annotations


class PaperBrokerNotReady(NotImplementedError):
    """Phase 2 has not landed yet."""


def run_paper(*args, **kwargs):  # noqa: ANN002, ANN003
    raise PaperBrokerNotReady(
        "The paper broker lands in Phase 2, after the backtester. Live trading "
        "is gated on a positive paper-trading sample."
    )
