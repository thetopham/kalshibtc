from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..market.state import MarketState


@dataclass(frozen=True)
class Signal:
    side: str  # "long_above", "long_below", or "none"
    reason: str
    confidence: float
    strategy: str = "manual"
    target_notional: float | None = None
    estimated_shares: float | None = None
    features: dict[str, float | bool | str | None] | None = None
    allow_price_strike_mismatch: bool = False


class Strategy(Protocol):
    @property
    def name(self) -> str: ...

    def on_tick(self, state: MarketState) -> Signal: ...
