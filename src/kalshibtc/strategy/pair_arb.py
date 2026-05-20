from __future__ import annotations

from dataclasses import dataclass

from ..market.state import MarketState
from .signals import Signal


@dataclass(frozen=True)
class PairArbStrategy:
    """Slope/distance/time entry filter for paired-leg paper inventory.

    The strategy's signal is only used to seed inventory. Pair construction and
    hedge decisions are handled by InventoryManager/HedgeManager so the primary
    objective is complete Above+Below pairs below the configured total cost.
    """

    name: str = "pair_arb"
    min_abs_distance_from_strike: float = 10.0
    min_abs_slope: float = 0.5
    min_seconds_to_close: float = 30.0
    max_seconds_to_close: float = 14 * 60.0

    def on_tick(self, state: MarketState) -> Signal:
        seconds = state.seconds_to_close
        if seconds < self.min_seconds_to_close:
            return Signal("none", "too close to expiry for pair inventory entry", 0.0, strategy=self.name)
        if seconds > self.max_seconds_to_close:
            return Signal("none", "too early for pair inventory entry", 0.0, strategy=self.name)
        slope = state.slope_30s
        if slope is None or abs(slope) < self.min_abs_slope:
            return Signal("none", "slope filter blocked pair inventory entry", 0.0, strategy=self.name)
        distance = state.distance_from_strike
        if abs(distance) < self.min_abs_distance_from_strike:
            return Signal("none", "distance filter blocked pair inventory entry", 0.0, strategy=self.name)
        if distance > 0 and slope > 0:
            return Signal("long_above", "pair inventory seed: above strike with positive slope", 0.72, strategy=self.name)
        if distance < 0 and slope < 0:
            return Signal("long_below", "pair inventory seed: below strike with negative slope", 0.72, strategy=self.name)
        return Signal("none", "direction/slope not aligned for pair inventory entry", 0.0, strategy=self.name)
