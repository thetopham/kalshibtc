from __future__ import annotations

from dataclasses import dataclass

from ..market.state import MarketState
from .signals import Signal


@dataclass(frozen=True)
class BreakoutMomentumStrategy:
    """Trade only strong strike-side momentum with a tight top-of-book spread."""

    name: str = "breakout_momentum"
    min_abs_slope: float = 1.0
    max_min_spread: float = 0.04
    confidence_per_dollar_per_second: float = 0.015
    max_confidence: float = 0.9

    def on_tick(self, state: MarketState) -> Signal:
        slope = state.slope_30s
        if slope is None:
            return Signal("none", "missing 30s slope", 0.0, strategy=self.name)
        spread = state.orderbook.min_spread
        if spread is None or spread > self.max_min_spread:
            return Signal("none", "spread too wide for breakout", 0.0, strategy=self.name)
        if abs(slope) < self.min_abs_slope:
            return Signal("none", "slope too weak for breakout", 0.0, strategy=self.name)
        confidence = min(self.max_confidence, 0.5 + abs(slope) * self.confidence_per_dollar_per_second)
        if state.price > state.strike and slope > 0:
            return Signal("long_above", "breakout above strike with momentum", confidence, strategy=self.name)
        if state.price < state.strike and slope < 0:
            return Signal("long_below", "breakout below strike with momentum", confidence, strategy=self.name)
        return Signal("none", "breakout not aligned", 0.0, strategy=self.name)
