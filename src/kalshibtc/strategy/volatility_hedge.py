from __future__ import annotations

from dataclasses import dataclass

from ..market.state import MarketState
from .signals import Signal


@dataclass(frozen=True)
class VolatilityHedgeStrategy:
    name: str = "volatility_hedge"

    def on_tick(self, state: MarketState) -> Signal:
        return Signal("none", "VolatilityHedgeStrategy uses paper-only paired inventory manager", 0.0, strategy=self.name)
