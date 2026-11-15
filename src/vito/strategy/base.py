"""Strategy interface.

Strategies are pure functions over market data. They emit *signals*, never
orders — the risk layer decides whether a signal is actionable. Keeping them
side-effect free is what makes them backtestable and testable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import pandas as pd


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


@dataclass(frozen=True, slots=True)
class Signal:
    """A proposed trade idea, before sizing."""

    inst_id: str
    side: Side
    stop_distance: float  # fraction of price: 0.01 == 1%
    conviction: bool = False
    reason: str = ""


class Strategy(Protocol):
    """A strategy turns candles into signals."""

    name: str

    def generate(self, inst_id: str, candles: pd.DataFrame) -> Sequence[Signal]:
        """Return zero or more signals for the most recent bar.

        ``candles`` is expected to have columns ``timestamp, open, high, low,
        close, volume`` in ascending time order.
        """
        ...
