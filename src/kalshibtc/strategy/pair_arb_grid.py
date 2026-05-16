from __future__ import annotations

from dataclasses import dataclass

from ..market.state import MarketState
from .signals import Signal


@dataclass(frozen=True)
class PairArbGridStrategy:
    """Signal shell for the paper-only paired-leg volatility grid."""

    name: str = "pair_arb_grid"

    def on_tick(self, state: MarketState) -> Signal:
        return Signal("none", "pair_arb_grid uses orderbook grid manager, not directional entries", 0.0, strategy=self.name)
