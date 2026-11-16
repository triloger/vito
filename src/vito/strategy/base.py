"""Strategy interface.

A strategy is a pure function of history: it turns candles into a per-bar frame
of *intent*. It never sees the future, never sizes a position, and never places
an order — the engine and the risk policy decide the rest.

Signals are computed on the close of bar *i* and acted on at the open of bar
*i+1*, which is why the engine cannot accidentally use same-bar information.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

import pandas as pd


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


SIGNAL_COLUMNS = ("side", "stop_distance", "conviction")


class Strategy(Protocol):
    """Turns candles into a signal frame aligned with the candle index."""

    name: str

    def signals(self, inst_id: str, candles: pd.DataFrame) -> pd.DataFrame:
        """Return columns ``side``, ``stop_distance``, ``conviction``.

        ``side`` is ``long``/``short``/``flat``; ``stop_distance`` is a fraction
        of price (0.01 == 1%); ``conviction`` opts into the policy's higher risk
        tier where the instrument allows it.
        """
        ...
