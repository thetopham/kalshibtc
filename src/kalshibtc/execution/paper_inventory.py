from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Side = Literal["YES", "NO"]
Winner = Literal["YES", "NO"]


@dataclass(frozen=True)
class InventoryFill:
    side: Side
    price: float
    shares: float
    ts: datetime
    fee: float = 0.0
    reason: str = ""

    @property
    def cost(self) -> float:
        return self.price * self.shares + self.fee

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts.isoformat(),
            "side": self.side,
            "price": self.price,
            "shares": self.shares,
            "fee": self.fee,
            "cost": self.cost,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SettlementResult:
    winner: Winner
    settlement_value: float
    total_cost: float
    pnl: float


@dataclass
class PaperInventoryPosition:
    """Read-only/paper YES+NO inventory for one binary Kalshi market.

    This is accounting only. It has no broker dependency and cannot submit live orders.
    """

    market_ticker: str
    yes_shares: float = 0.0
    no_shares: float = 0.0
    yes_cost: float = 0.0
    no_cost: float = 0.0
    fills: list[InventoryFill] = field(default_factory=list)
    initial_entry_done: bool = False
    initial_side: Side | None = None
    last_fill_ts: datetime | None = None
    realized_pnl: float | None = None
    settlement_value: float | None = None
    final_winner: Winner | None = None

    def add_fill(
        self,
        *,
        side: Side,
        price: float,
        shares: float,
        ts: datetime,
        fee: float = 0.0,
        reason: str = "",
    ) -> InventoryFill:
        if side not in {"YES", "NO"}:
            raise ValueError(f"unsupported side: {side}")
        if price <= 0 or price > 1:
            raise ValueError(f"paper fill price must be within (0, 1]: {price}")
        if shares <= 0:
            raise ValueError(f"paper fill shares must be positive: {shares}")
        fill = InventoryFill(side=side, price=price, shares=shares, ts=ts, fee=fee, reason=reason)
        if side == "YES":
            self.yes_shares += shares
            self.yes_cost += fill.cost
        else:
            self.no_shares += shares
            self.no_cost += fill.cost
        if not self.initial_entry_done:
            self.initial_entry_done = True
            self.initial_side = side
        self.last_fill_ts = ts
        self.fills.append(fill)
        return fill

    @property
    def total_cost(self) -> float:
        return round(self.yes_cost + self.no_cost, 10)

    @property
    def total_shares(self) -> float:
        return self.yes_shares + self.no_shares

    @property
    def yes_avg(self) -> float:
        return self.yes_cost / self.yes_shares if self.yes_shares else 0.0

    @property
    def no_avg(self) -> float:
        return self.no_cost / self.no_shares if self.no_shares else 0.0

    @property
    def matched_shares(self) -> float:
        return min(self.yes_shares, self.no_shares)

    @property
    def unmatched_yes(self) -> float:
        return max(self.yes_shares - self.no_shares, 0.0)

    @property
    def unmatched_no(self) -> float:
        return max(self.no_shares - self.yes_shares, 0.0)

    @property
    def directional_exposure(self) -> float:
        return self.yes_shares - self.no_shares

    @property
    def locked_payout(self) -> float:
        return self.matched_shares * 1.0

    @property
    def locked_cost(self) -> float:
        return round(self.matched_shares * self.yes_avg + self.matched_shares * self.no_avg, 10)

    @property
    def locked_edge(self) -> float:
        return round(self.locked_payout - self.locked_cost, 10)

    @property
    def locked_edge_per_matched_share(self) -> float:
        return self.locked_edge / self.matched_shares if self.matched_shares else 0.0

    @property
    def max_payout_if_yes(self) -> float:
        return self.yes_shares * 1.0

    @property
    def max_payout_if_no(self) -> float:
        return self.no_shares * 1.0

    @property
    def worst_case_settlement_value(self) -> float:
        return min(self.max_payout_if_yes, self.max_payout_if_no)

    def worst_case_pnl(self) -> float:
        return round(self.worst_case_settlement_value - self.total_cost, 10)

    def mark_value(self, *, yes_bid: float | None, no_bid: float | None) -> float | None:
        if yes_bid is None or no_bid is None:
            return None
        return self.yes_shares * yes_bid + self.no_shares * no_bid

    def settle(self, winner: Winner) -> SettlementResult:
        if winner not in {"YES", "NO"}:
            raise ValueError(f"unsupported settlement winner: {winner}")
        settlement_value = self.max_payout_if_yes if winner == "YES" else self.max_payout_if_no
        pnl = round(settlement_value - self.total_cost, 10)
        self.final_winner = winner
        self.settlement_value = settlement_value
        self.realized_pnl = pnl
        return SettlementResult(
            winner=winner,
            settlement_value=settlement_value,
            total_cost=self.total_cost,
            pnl=pnl,
        )

    def summary_dict(self) -> dict[str, Any]:
        return {
            "market_ticker": self.market_ticker,
            "initial_side": self.initial_side or "",
            "yes_shares": self.yes_shares,
            "no_shares": self.no_shares,
            "yes_avg": self.yes_avg,
            "no_avg": self.no_avg,
            "yes_cost": self.yes_cost,
            "no_cost": self.no_cost,
            "total_cost": self.total_cost,
            "matched_shares": self.matched_shares,
            "directional_exposure": self.directional_exposure,
            "locked_edge": self.locked_edge,
            "locked_edge_per_matched_share": self.locked_edge_per_matched_share,
            "unmatched_yes": self.unmatched_yes,
            "unmatched_no": self.unmatched_no,
            "max_payout_if_yes": self.max_payout_if_yes,
            "max_payout_if_no": self.max_payout_if_no,
            "worst_case_settlement_value": self.worst_case_settlement_value,
            "worst_case_pnl": self.worst_case_pnl(),
            "final_winner": self.final_winner or "",
            "settlement_value": self.settlement_value,
            "pnl": self.realized_pnl,
            "fills": jsonable_fills(self.fills),
        }


def jsonable_fills(fills: list[InventoryFill]) -> list[dict[str, Any]]:
    return [fill.as_dict() for fill in fills]
