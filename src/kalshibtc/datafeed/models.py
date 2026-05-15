from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Tick:
    """One BTC websocket observation."""

    ts: datetime
    price: float
    bid: float | None = None
    ask: float | None = None
    source: str = "websocket"
    symbol: str = "BTC-USD"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderBookSnapshot:
    """Top-of-book Kalshi binary market snapshot.

    Kalshi YES/NO asks are the executable prices for long_above/long_below.
    """

    ts: datetime
    market_ticker: str
    yes_bid: float | None = None
    yes_ask: float | None = None
    no_bid: float | None = None
    no_ask: float | None = None
    sequence: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def yes_mid(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2.0

    @property
    def no_mid(self) -> float | None:
        if self.no_bid is None or self.no_ask is None:
            return None
        return (self.no_bid + self.no_ask) / 2.0

    @property
    def yes_spread(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid

    @property
    def no_spread(self) -> float | None:
        if self.no_bid is None or self.no_ask is None:
            return None
        return self.no_ask - self.no_bid

    @property
    def min_spread(self) -> float | None:
        spreads = [spread for spread in (self.yes_spread, self.no_spread) if spread is not None]
        return min(spreads) if spreads else None

    @property
    def is_crossed(self) -> bool:
        return (
            self.yes_bid is not None
            and self.no_bid is not None
            and self.yes_bid + self.no_bid > 1.0
        )
