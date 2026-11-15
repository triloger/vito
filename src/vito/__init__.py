"""Vito — a personal quantitative treasury system for OKX.

Three layers, deliberately separated:

* ``vito.data`` / ``vito.strategy`` / ``vito.backtest`` / ``vito.paper``
  — research. No money at risk.
* ``vito.policy`` / ``vito.risk``
  — an enforced constitution. Every order must pass it.
* ``vito.exec``
  — order routing. Disabled by default, gated behind an explicit arm step.

The package is safe to import and run with no API keys: everything degrades
to public market data.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
