from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class HedgeFill:
    side: str
    price: float
    contracts: float
    ts: datetime
    reason: str


@dataclass
class HedgePosition:
    """Paper-only YES/NO inventory for a single Kalshi BTC 15m market."""

    market_ticker: str
    fills: list[HedgeFill] = field(default_factory=list)

    def add_fill(
        self,
        *,
        side: str,
        price: float,
        contracts: float,
        ts: datetime,
        reason: str,
    ) -> None:
        if side not in {"yes", "no"}:
            raise ValueError(f"unsupported hedge side: {side}")
        if not 0.0 < price < 1.0:
            raise ValueError(f"price must be between 0 and 1: {price}")
        if contracts <= 0.0:
            raise ValueError(f"contracts must be positive: {contracts}")
        self.fills.append(
            HedgeFill(
                side=side,
                price=float(price),
                contracts=float(contracts),
                ts=ts,
                reason=reason,
            )
        )

    @property
    def yes_contracts(self) -> float:
        return self._contracts("yes")

    @property
    def no_contracts(self) -> float:
        return self._contracts("no")

    @property
    def avg_yes_entry(self) -> float | None:
        return self._average_price("yes")

    @property
    def avg_no_entry(self) -> float | None:
        return self._average_price("no")

    @property
    def paired_contracts(self) -> float:
        return min(self.yes_contracts, self.no_contracts)

    @property
    def combined_average_cost(self) -> float | None:
        if self.avg_yes_entry is None or self.avg_no_entry is None:
            return None
        return self.avg_yes_entry + self.avg_no_entry

    @property
    def locked_edge_per_pair(self) -> float | None:
        if self.combined_average_cost is None:
            return None
        return 1.0 - self.combined_average_cost

    def would_improve(self, *, side: str, price: float) -> bool:
        current = self._average_price(side)
        return current is None or price < current

    def projected_combined_average_cost(
        self,
        *,
        side: str,
        price: float,
        contracts: float,
    ) -> float | None:
        yes_contracts = self.yes_contracts
        no_contracts = self.no_contracts
        yes_cost = (self.avg_yes_entry or 0.0) * yes_contracts
        no_cost = (self.avg_no_entry or 0.0) * no_contracts
        if side == "yes":
            yes_contracts += contracts
            yes_cost += price * contracts
        elif side == "no":
            no_contracts += contracts
            no_cost += price * contracts
        else:
            raise ValueError(f"unsupported hedge side: {side}")
        if yes_contracts <= 0.0 or no_contracts <= 0.0:
            return None
        return (yes_cost / yes_contracts) + (no_cost / no_contracts)

    def _contracts(self, side: str) -> float:
        return sum(fill.contracts for fill in self.fills if fill.side == side)

    def _average_price(self, side: str) -> float | None:
        fills = [fill for fill in self.fills if fill.side == side]
        contracts = sum(fill.contracts for fill in fills)
        if contracts <= 0.0:
            return None
        return sum(fill.price * fill.contracts for fill in fills) / contracts
