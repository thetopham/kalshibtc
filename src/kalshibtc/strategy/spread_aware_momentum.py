from __future__ import annotations

from dataclasses import dataclass

from ..market.pricing import entry_price_for_signal, spread_for_signal
from ..market.state import MarketState
from .signals import Signal


@dataclass(frozen=True)
class SpreadAwareMomentumStrategy:
    """Momentum strategy gated by cheap executable side and max side spread."""

    name: str = "spread_aware_momentum"
    min_abs_slope: float = 0.75
    max_spread: float = 0.03
    max_entry_price: float = 0.60
    confidence_per_dollar_per_second: float = 0.012
    max_confidence: float = 0.9

    def on_tick(self, state: MarketState) -> Signal:
        slope = state.slope_30s
        if slope is None:
            return Signal("none", "missing 30s slope", 0.0, strategy=self.name)
        if abs(slope) < self.min_abs_slope:
            return Signal("none", "momentum too weak", 0.0, strategy=self.name)
        side = "none"
        if state.price > state.strike and slope > 0:
            side = "long_above"
        elif state.price < state.strike and slope < 0:
            side = "long_below"
        if side == "none":
            return Signal("none", "momentum not aligned", 0.0, strategy=self.name)
        spread = spread_for_signal(side, state.orderbook)
        entry = entry_price_for_signal(side, state.orderbook)
        if spread is None or spread > self.max_spread:
            return Signal("none", "side spread too wide", 0.0, strategy=self.name)
        if entry is None or entry > self.max_entry_price:
            return Signal("none", "momentum side not cheap enough", 0.0, strategy=self.name)
        confidence = min(self.max_confidence, 0.5 + abs(slope) * self.confidence_per_dollar_per_second)
        return Signal(side, "spread-aware momentum aligned", confidence, strategy=self.name)
