from __future__ import annotations

from dataclasses import dataclass

from ..market.state import MarketState
from .signals import Signal


@dataclass(frozen=True)
class NoTradeBaselineStrategy:
    """Control strategy for replay/dashboard comparisons."""

    name: str = "no_trade_baseline"

    def on_tick(self, state: MarketState) -> Signal:
        return Signal("none", "control strategy: no trade", 0.0, strategy=self.name)
