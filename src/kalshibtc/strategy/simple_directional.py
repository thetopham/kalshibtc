from __future__ import annotations

from dataclasses import dataclass

from ..market.state import MarketState
from .signals import Signal


@dataclass(frozen=True)
class SimpleDirectionalStrategy:
    """Boring v1 rule: above+trend up, below+trend down, otherwise no trade."""

    name: str = "simple_directional"
    min_abs_slope: float = 0.0
    confidence_per_dollar_per_second: float = 0.01
    max_confidence: float = 0.90

    def on_tick(self, state: MarketState) -> Signal:
        slope = state.slope_30s
        if slope is None:
            return Signal("none", "missing 30s slope", 0.0, strategy=self.name)
        confidence = min(self.max_confidence, 0.5 + abs(slope) * self.confidence_per_dollar_per_second)
        if state.price > state.strike and slope > self.min_abs_slope:
            return Signal("long_above", "above strike + trend up", confidence, strategy=self.name)
        if state.price < state.strike and slope < -self.min_abs_slope:
            return Signal("long_below", "below strike + trend down", confidence, strategy=self.name)
        return Signal("none", "no alignment", 0.0, strategy=self.name)
