from __future__ import annotations

from ..market.state import MarketState
from .signals import Signal


class InventoryVolRebalanceStrategy:
    """Registry marker for the paper-only inventory volatility rebalancer.

    The actual accounting/rebalancing engine runs through InventoryVolRebalancePaperTrader
    because it needs persistent per-contract inventory, reductions, and research metrics.
    This strategy object intentionally emits no normal directional fill signals.
    """

    @property
    def name(self) -> str:
        return "inventory_vol_rebalance"

    def on_tick(self, state: MarketState) -> Signal:
        return Signal(
            side="none",
            reason="paper-only inventory volatility rebalancer uses dedicated inventory manager; no live/order signal",
            confidence=0.0,
            strategy=self.name,
        )
