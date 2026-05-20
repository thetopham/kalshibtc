from __future__ import annotations

from dataclasses import dataclass

from ..market.state import MarketState
from .signals import Signal


@dataclass(frozen=True)
class PairArbPassiveStrategy:
    name: str = "pair_arb_passive"

    def on_tick(self, state: MarketState) -> Signal:
        return Signal("none", "pair_arb_passive uses passive limit-order simulation", 0.0, strategy=self.name)
