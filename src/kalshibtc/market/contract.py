from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ContractWindow:
    """The current Kalshi 15-minute above/below contract window."""

    ticker: str
    strike: float
    close_time: datetime
    open_time: datetime | None = None

    def signed_distance(self, price: float) -> float:
        return float(price) - float(self.strike)

    def is_above(self, price: float) -> bool:
        return self.signed_distance(price) > 0.0

    def side_for_price(self, price: float) -> str:
        distance = self.signed_distance(price)
        if distance > 0.0:
            return "above"
        if distance < 0.0:
            return "below"
        return "at"

    def seconds_to_close(self, at: datetime) -> float:
        return (self.close_time - at).total_seconds()
