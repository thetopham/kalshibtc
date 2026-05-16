from __future__ import annotations

from dataclasses import dataclass

from ..market.state import MarketState
from .signals import Signal


@dataclass(frozen=True)
class MeanReversionToStrikeStrategy:
    """Fade extreme distance from strike when the latest slope is decelerating toward it."""

    name: str = "mean_reversion_to_strike"
    min_distance: float = 50.0
    max_abs_slope: float = 1.0
    confidence_per_dollar: float = 0.002
    max_confidence: float = 0.85

    def on_tick(self, state: MarketState) -> Signal:
        slope = state.slope_30s
        if slope is None:
            return Signal("none", "missing 30s slope", 0.0, strategy=self.name)
        distance = state.distance_from_strike
        if abs(distance) < self.min_distance:
            return Signal("none", "distance from strike too small", 0.0, strategy=self.name)
        if abs(slope) > self.max_abs_slope:
            return Signal("none", "slope not decelerated", 0.0, strategy=self.name)
        confidence = min(self.max_confidence, 0.5 + abs(distance) * self.confidence_per_dollar)
        if distance > 0 and slope <= 0:
            return Signal("long_below", "above strike extreme fading toward strike", confidence, strategy=self.name)
        if distance < 0 and slope >= 0:
            return Signal("long_above", "below strike extreme fading toward strike", confidence, strategy=self.name)
        return Signal("none", "mean reversion not aligned", 0.0, strategy=self.name)
