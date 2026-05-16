from __future__ import annotations

from dataclasses import dataclass

from ..market.state import MarketState
from .signals import Signal


@dataclass(frozen=True)
class InventoryVolRegimeStrategy:
    name: str = "inventory_vol_regime"

    def on_tick(self, state: MarketState) -> Signal:
        return Signal("none", "inventory_vol_regime uses paper-only inventory manager", 0.0, strategy=self.name)
