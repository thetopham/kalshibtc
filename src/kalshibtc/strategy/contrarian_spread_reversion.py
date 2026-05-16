from __future__ import annotations

from dataclasses import dataclass

from ..market.pricing import spread_for_signal
from ..market.state import MarketState
from .signals import Signal


@dataclass(frozen=True)
class ContrarianSpreadReversionStrategy:
    """Buy the cheap opposite leg when the at-the-money side gets crowded.

    This is a research-only proxy for a market-making/hedge idea: when BTC is
    above strike and YES is expensive, buy cheap NO if its spread is tight; when
    BTC is below strike and NO is expensive, buy cheap YES. It intentionally
    emits ordinary long_above/long_below signals so existing replay/paper code
    can test the entry idea without live order placement or true exit/stop logic.
    """

    name: str = "contrarian_spread_reversion"
    min_distance: float = 25.0
    min_abs_slope: float = 0.5
    min_crowded_bid: float = 0.70
    max_cheap_entry_price: float = 0.30
    max_cheap_leg_spread: float = 0.04
    min_seconds_to_close: float = 30.0
    max_seconds_to_close: float = 12 * 60.0
    confidence_per_dollar: float = 0.001
    confidence_per_crowding: float = 0.35
    max_confidence: float = 0.88

    def on_tick(self, state: MarketState) -> Signal:
        if state.seconds_to_close < self.min_seconds_to_close:
            return Signal("none", "too close to close for contrarian hedge", 0.0, strategy=self.name)
        if state.seconds_to_close > self.max_seconds_to_close:
            return Signal("none", "too early for contrarian hedge", 0.0, strategy=self.name)
        slope = state.slope_30s
        if slope is None:
            return Signal("none", "missing 30s slope", 0.0, strategy=self.name)
        if abs(slope) < self.min_abs_slope:
            return Signal("none", "volatility too low for contrarian hedge", 0.0, strategy=self.name)
        distance = state.distance_from_strike
        if abs(distance) < self.min_distance:
            return Signal("none", "too close to strike for contrarian hedge", 0.0, strategy=self.name)

        if distance > 0:
            return self._signal_for_side(
                side="long_below",
                crowded_bid=state.orderbook.yes_bid,
                cheap_entry=state.orderbook.no_ask,
                cheap_spread=spread_for_signal("long_below", state.orderbook),
                reason="above strike: fade expensive YES by buying cheap NO",
                distance=distance,
            )
        return self._signal_for_side(
            side="long_above",
            crowded_bid=state.orderbook.no_bid,
            cheap_entry=state.orderbook.yes_ask,
            cheap_spread=spread_for_signal("long_above", state.orderbook),
            reason="below strike: fade expensive NO by buying cheap YES",
            distance=distance,
        )

    def _signal_for_side(
        self,
        *,
        side: str,
        crowded_bid: float | None,
        cheap_entry: float | None,
        cheap_spread: float | None,
        reason: str,
        distance: float,
    ) -> Signal:
        if crowded_bid is None or crowded_bid < self.min_crowded_bid:
            return Signal("none", "crowded leg not expensive enough", 0.0, strategy=self.name)
        if cheap_entry is None or cheap_entry > self.max_cheap_entry_price:
            return Signal("none", "opposite hedge leg not cheap enough", 0.0, strategy=self.name)
        if cheap_spread is None or cheap_spread > self.max_cheap_leg_spread:
            return Signal("none", "cheap hedge leg spread too wide", 0.0, strategy=self.name)
        crowding = max(0.0, crowded_bid - self.min_crowded_bid)
        confidence = min(
            self.max_confidence,
            0.55 + abs(distance) * self.confidence_per_dollar + crowding * self.confidence_per_crowding,
        )
        return Signal(side, reason, confidence, strategy=self.name)
