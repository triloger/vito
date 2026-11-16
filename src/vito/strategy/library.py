"""The strategy library.

Three families, deliberately different in character so the backtest can choose
between them rather than argue:

* ``trend``     — EMA cross, holds winners, few trades.
* ``breakout``  — Donchian channel, volume-confirmed.
* ``meanrev``   — z-score reversion, exits at the mean.

Each returns the same signal frame; each is a pure function of its history.
"""

from __future__ import annotations

import pandas as pd

from vito.strategy.indicators import atr, donchian, ema, sma, zscore


class _Base:
    name = "base"
    fast = 20
    slow = 50
    atr_window = 14
    atr_mult = 2.0

    def _frame(self, side: pd.Series, stop: pd.Series, conviction: pd.Series) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "side": side.fillna("flat").astype(str),
                "stop_distance": stop.astype(float),
                "conviction": conviction.fillna(False).astype(bool),
            }
        )


class TrendFollow(_Base):
    """Long above a rising slow EMA, short below a falling one.

    Stops are ATR-scaled, so the position shrinks automatically when volatility
    expands. That is most of the risk management; the policy does the rest.
    """

    name = "trend"

    def __init__(self, fast: int = 20, slow: int = 50, atr_mult: float = 2.0) -> None:
        self.fast, self.slow, self.atr_mult = fast, slow, atr_mult

    def signals(self, inst_id: str, candles: pd.DataFrame) -> pd.DataFrame:
        close, high, low = candles["close"], candles["high"], candles["low"]
        fast = ema(close, self.fast)
        slow = ema(close, self.slow)
        slope = slow.diff()

        side = pd.Series("flat", index=close.index, dtype=object)
        side[(fast > slow) & (slope > 0)] = "long"
        side[(fast < slow) & (slope < 0)] = "short"

        stop = (atr(high, low, close, self.atr_window) * self.atr_mult) / close
        conviction = (fast - slow).abs() / close > 0.01
        return self._frame(side, stop, conviction)


class Breakout(_Base):
    """Donchian channel breakout, confirmed by volume expansion.

    Volume confirmation is the conviction tier: breakouts on thin volume are the
    ones that fail back through the channel.
    """

    name = "breakout"

    def __init__(self, window: int = 20, atr_mult: float = 2.0, volume_mult: float = 1.5) -> None:
        self.window, self.atr_mult, self.volume_mult = window, atr_mult, volume_mult

    def signals(self, inst_id: str, candles: pd.DataFrame) -> pd.DataFrame:
        close, high, low, volume = (
            candles["close"],
            candles["high"],
            candles["low"],
            candles["volume"],
        )
        upper, lower = donchian(high, low, self.window)

        side = pd.Series("flat", index=close.index, dtype=object)
        side[close > upper] = "long"
        side[close < lower] = "short"

        stop = (atr(high, low, close, self.atr_window) * self.atr_mult) / close
        vol_ma = sma(volume, 20)
        conviction = volume > vol_ma * self.volume_mult
        return self._frame(side, stop, conviction)


class MeanReversion(_Base):
    """Fade stretches away from the mean, exit when the stretch is gone.

    Structurally the opposite of trend following: high win rate, small wins, and
    the occasional large loss. Worth testing precisely because it fails in
    different regimes.
    """

    name = "meanrev"

    def __init__(self, window: int = 48, entry_z: float = 2.0, exit_z: float = 0.5) -> None:
        self.window, self.entry_z, self.exit_z = window, entry_z, exit_z

    def signals(self, inst_id: str, candles: pd.DataFrame) -> pd.DataFrame:
        close, high, low = candles["close"], candles["high"], candles["low"]
        z = zscore(close, self.window)

        side = pd.Series("flat", index=close.index, dtype=object)
        side[z < -self.entry_z] = "long"
        side[z > self.entry_z] = "short"
        side[z.abs() < self.exit_z] = "flat"

        stop = (atr(high, low, close, self.atr_window) * 2.5) / close
        conviction = z.abs() > 2.5
        return self._frame(side, stop, conviction)


STRATEGIES = {
    "trend": TrendFollow,
    "breakout": Breakout,
    "meanrev": MeanReversion,
}


def build(name: str, **kwargs: object) -> _Base:
    try:
        factory = STRATEGIES[name]
    except KeyError:
        raise ValueError(f"unknown strategy: {name!r} (have {sorted(STRATEGIES)})") from None
    return factory(**kwargs)  # type: ignore[arg-type]
