from __future__ import annotations

from dataclasses import dataclass

from ..market.pricing import entry_price_for_signal, spread_for_signal
from ..market.state import MarketState
from .signals import Signal


@dataclass(frozen=True)
class LateWindowOnlyStrategy:
    """High-conviction expiry hold strategy with strict late-entry filters."""

    name: str = "late_window_only"
    max_seconds_to_close: float = 60.0
    min_distance: float = 10.0
    max_entry_price: float = 0.90
    max_entry_spread: float = 0.05
    base_confidence: float = 0.65
    max_confidence: float = 0.95

    def on_tick(self, state: MarketState) -> Signal:
        seconds = state.seconds_to_close
        if seconds > self.max_seconds_to_close:
            return Signal("none", "outside final entry window", 0.0, strategy=self.name)
        if seconds <= 0:
            return Signal("none", "market already closed", 0.0, strategy=self.name)

        distance = state.distance_from_strike
        if abs(distance) < self.min_distance:
            return Signal("none", "too close to strike in late window", 0.0, strategy=self.name)

        side = "long_above" if distance > 0 else "long_below"
        entry_price = entry_price_for_signal(side, state.orderbook)
        if entry_price is None:
            return Signal("none", "missing late window entry price", 0.0, strategy=self.name)
        if entry_price > self.max_entry_price:
            return Signal("none", "late window price too expensive", 0.0, strategy=self.name)

        spread = spread_for_signal(side, state.orderbook)
        if spread is None:
            return Signal("none", "missing late window spread", 0.0, strategy=self.name)
        if spread > self.max_entry_spread:
            return Signal("none", "late window spread too wide", 0.0, strategy=self.name)

        distance_component = min(abs(distance), 200.0) / 400.0
        time_component = max(0.0, self.max_seconds_to_close - seconds) / 200.0
        confidence = min(self.max_confidence, self.base_confidence + distance_component + time_component)
        direction = "above" if side == "long_above" else "below"
        return Signal(side, f"final minute {direction} strike with tradable book", confidence, strategy=self.name)
