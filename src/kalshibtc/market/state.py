from __future__ import annotations

from dataclasses import dataclass

from ..datafeed.models import OrderBookSnapshot, Tick
from .contract import ContractWindow


@dataclass(frozen=True)
class MarketState:
    """Normalized per-tick state consumed by strategies and risk filters."""

    tick: Tick
    orderbook: OrderBookSnapshot
    contract: ContractWindow
    slope_30s: float | None

    @property
    def price(self) -> float:
        return self.tick.price

    @property
    def strike(self) -> float:
        return self.contract.strike

    @property
    def distance_from_strike(self) -> float:
        return self.contract.signed_distance(self.price)

    @property
    def abs_distance_from_strike(self) -> float:
        return abs(self.distance_from_strike)

    @property
    def is_above_strike(self) -> bool:
        return self.distance_from_strike > 0.0

    @property
    def seconds_to_close(self) -> float:
        return self.contract.seconds_to_close(self.tick.ts)
