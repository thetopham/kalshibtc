from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

GridSide = Literal["ABOVE", "BELOW"]


@dataclass(frozen=True)
class PairArbGridConfig:
    starter_qty: float = 5.0
    grid_qty: float = 5.0
    max_leg_qty: float = 100.0
    max_imbalance_qty: float = 25.0
    starter_pair_threshold: float = 1.00
    add_pair_threshold: float = 0.95
    cheap_leg_trigger: float = 0.35
    stop_adding_seconds_to_expiry: float = 60.0
    slippage: float = 0.0
    fee_per_contract: float = 0.0


@dataclass(frozen=True)
class GridFill:
    event_key: str
    market_ticker: str
    side: GridSide
    price: float
    quantity: float
    fee: float
    ts: datetime
    reason: str

    @property
    def cost(self) -> float:
        return round(self.price * self.quantity + self.fee, 10)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_key": self.event_key,
            "market_ticker": self.market_ticker,
            "side": self.side,
            "price": self.price,
            "quantity": self.quantity,
            "fee": self.fee,
            "cost": self.cost,
            "ts": self.ts.isoformat(),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class GridDecision:
    fills: int
    reason: str
    fill_events: list[GridFill] = field(default_factory=list)


@dataclass
class PairArbGridPosition:
    event_key: str
    market_ticker: str
    market_close_time: str
    strike: float
    held_above_qty: float = 0.0
    held_below_qty: float = 0.0
    above_cost: float = 0.0
    below_cost: float = 0.0
    last_seconds_to_close: float = 0.0
    last_distance_from_strike: float = 0.0
    fills: list[GridFill] = field(default_factory=list)

    @property
    def held_above_avg(self) -> float:
        return round(self.above_cost / self.held_above_qty, 10) if self.held_above_qty else 0.0

    @property
    def held_below_avg(self) -> float:
        return round(self.below_cost / self.held_below_qty, 10) if self.held_below_qty else 0.0

    @property
    def paired_qty(self) -> float:
        return min(self.held_above_qty, self.held_below_qty)

    @property
    def unpaired_directional_exposure(self) -> float:
        return abs(self.held_above_qty - self.held_below_qty)

    @property
    def pair_cost(self) -> float | None:
        if not self.held_above_qty or not self.held_below_qty:
            return None
        return round(self.held_above_avg + self.held_below_avg, 10)

    @property
    def locked_profit(self) -> float:
        if self.pair_cost is None:
            return 0.0
        return round(self.paired_qty * (1.0 - self.pair_cost), 10)

    @property
    def total_cost(self) -> float:
        return round(self.above_cost + self.below_cost, 10)

    @property
    def worst_case_settlement_value(self) -> float:
        return min(self.held_above_qty, self.held_below_qty)

    @property
    def max_capital_at_risk(self) -> float:
        return self.total_cost

    @property
    def locked_profit_to_max_capital_at_risk(self) -> float:
        if self.max_capital_at_risk <= 0:
            return 0.0
        return round(self.locked_profit / self.max_capital_at_risk, 10)

    def add_fill(self, *, side: GridSide, price: float, quantity: float, fee: float, ts: datetime, reason: str) -> GridFill:
        fill = GridFill(self.event_key, self.market_ticker, side, price, quantity, fee, ts, reason)
        if side == "ABOVE":
            self.held_above_qty += quantity
            self.above_cost += fill.cost
        else:
            self.held_below_qty += quantity
            self.below_cost += fill.cost
        self.fills.append(fill)
        return fill

    def projected_pair_cost_for(self, side: GridSide, price: float, quantity: float) -> float:
        above_qty = self.held_above_qty + (quantity if side == "ABOVE" else 0.0)
        below_qty = self.held_below_qty + (quantity if side == "BELOW" else 0.0)
        above_cost = self.above_cost + (price * quantity if side == "ABOVE" else 0.0)
        below_cost = self.below_cost + (price * quantity if side == "BELOW" else 0.0)
        if not above_qty or not below_qty:
            return 999.0
        return round(above_cost / above_qty + below_cost / below_qty, 10)

    def projected_imbalance_for(self, side: GridSide, quantity: float) -> float:
        above = self.held_above_qty + (quantity if side == "ABOVE" else 0.0)
        below = self.held_below_qty + (quantity if side == "BELOW" else 0.0)
        return abs(above - below)

    def summary_dict(self) -> dict[str, Any]:
        return {
            "event_key": self.event_key,
            "market_ticker": self.market_ticker,
            "market_close_time": self.market_close_time,
            "strike": self.strike,
            "held_above_qty": self.held_above_qty,
            "held_above_avg": self.held_above_avg,
            "held_below_qty": self.held_below_qty,
            "held_below_avg": self.held_below_avg,
            "pair_cost": self.pair_cost,
            "locked_profit": self.locked_profit,
            "paired_qty": self.paired_qty,
            "unpaired_directional_exposure": self.unpaired_directional_exposure,
            "total_cost": self.total_cost,
            "max_capital_at_risk": self.max_capital_at_risk,
            "locked_profit_to_max_capital_at_risk": self.locked_profit_to_max_capital_at_risk,
            "time_to_expiry": self.last_seconds_to_close,
            "distance_from_strike": self.last_distance_from_strike,
            "fills_json": [fill.as_dict() for fill in self.fills],
        }


class PairArbGridManager:
    def __init__(self, config: PairArbGridConfig | None = None) -> None:
        self.config = config or PairArbGridConfig()
        self.positions: dict[str, PairArbGridPosition] = {}
        self.events: list[dict[str, Any]] = []

    def on_book(
        self,
        *,
        event_key: str,
        market_ticker: str,
        ts: datetime,
        market_close_time: str,
        strike: float,
        seconds_to_close: float,
        distance_from_strike: float,
        above_ask: float | None,
        below_ask: float | None,
    ) -> GridDecision:
        if seconds_to_close <= self.config.stop_adding_seconds_to_expiry:
            self._log(event_key, market_ticker, ts, "NONE", None, 0.0, "stop_adding_near_expiry")
            return GridDecision(0, "stop_adding_near_expiry")
        if above_ask is None or below_ask is None or above_ask <= 0 or below_ask <= 0:
            self._log(event_key, market_ticker, ts, "NONE", None, 0.0, "invalid_book")
            return GridDecision(0, "invalid_book")
        position = self.positions.setdefault(
            event_key,
            PairArbGridPosition(event_key=event_key, market_ticker=market_ticker, market_close_time=market_close_time, strike=strike),
        )
        position.last_seconds_to_close = seconds_to_close
        position.last_distance_from_strike = distance_from_strike
        if not position.held_above_qty and not position.held_below_qty:
            return self._try_starter(position=position, ts=ts, above_ask=above_ask, below_ask=below_ask)
        return self._try_grid_add(position=position, ts=ts, above_ask=above_ask, below_ask=below_ask)

    def _try_starter(self, *, position: PairArbGridPosition, ts: datetime, above_ask: float, below_ask: float) -> GridDecision:
        above_price = round(above_ask + self.config.slippage, 10)
        below_price = round(below_ask + self.config.slippage, 10)
        starter_cost = above_price + below_price
        if starter_cost > self.config.starter_pair_threshold:
            self._log(position.event_key, position.market_ticker, ts, "BOTH", starter_cost, 0.0, "starter_pair_cost_above_threshold")
            return GridDecision(0, "starter_pair_cost_above_threshold")
        fills = [
            position.add_fill(side="ABOVE", price=above_price, quantity=self.config.starter_qty, fee=self.config.starter_qty * self.config.fee_per_contract, ts=ts, reason="starter_pair"),
            position.add_fill(side="BELOW", price=below_price, quantity=self.config.starter_qty, fee=self.config.starter_qty * self.config.fee_per_contract, ts=ts, reason="starter_pair"),
        ]
        for fill in fills:
            self._log(position.event_key, position.market_ticker, ts, fill.side, starter_cost, fill.quantity, "starter_pair", fill=fill)
        return GridDecision(2, "starter_pair", fills)

    def _try_grid_add(self, *, position: PairArbGridPosition, ts: datetime, above_ask: float, below_ask: float) -> GridDecision:
        candidates: list[tuple[GridSide, float]] = []
        if below_ask <= self.config.cheap_leg_trigger:
            candidates.append(("BELOW", below_ask))
        if above_ask <= self.config.cheap_leg_trigger:
            candidates.append(("ABOVE", above_ask))
        if not candidates:
            self._log(position.event_key, position.market_ticker, ts, "NONE", None, 0.0, "no_cheap_underweight_leg")
            return GridDecision(0, "no_cheap_underweight_leg")
        side, ask = min(candidates, key=lambda item: item[1])
        if (side == "ABOVE" and position.held_above_qty + self.config.grid_qty > self.config.max_leg_qty) or (
            side == "BELOW" and position.held_below_qty + self.config.grid_qty > self.config.max_leg_qty
        ):
            self._log(position.event_key, position.market_ticker, ts, side, None, 0.0, "max_leg_qty")
            return GridDecision(0, "max_leg_qty")
        if position.projected_imbalance_for(side, self.config.grid_qty) > self.config.max_imbalance_qty:
            self._log(position.event_key, position.market_ticker, ts, side, None, 0.0, "max_imbalance_qty")
            return GridDecision(0, "max_imbalance_qty")
        price = round(ask + self.config.slippage, 10)
        projected_pair_cost = position.projected_pair_cost_for(side, price, self.config.grid_qty)
        if projected_pair_cost > self.config.add_pair_threshold:
            self._log(position.event_key, position.market_ticker, ts, side, projected_pair_cost, 0.0, "projected_pair_cost_above_threshold")
            return GridDecision(0, "projected_pair_cost_above_threshold")
        fill = position.add_fill(side=side, price=price, quantity=self.config.grid_qty, fee=self.config.grid_qty * self.config.fee_per_contract, ts=ts, reason="grid_add_cheap_underweight_leg")
        self._log(position.event_key, position.market_ticker, ts, side, projected_pair_cost, fill.quantity, "grid_add_cheap_underweight_leg", fill=fill)
        return GridDecision(1, "grid_add_cheap_underweight_leg", [fill])

    def _log(self, event_key: str, market_ticker: str, ts: datetime, side: str, projected_pair_cost: float | None, quantity: float, reason: str, fill: GridFill | None = None) -> None:
        self.events.append(
            {
                "event_key": event_key,
                "market_ticker": market_ticker,
                "ts": ts.isoformat(),
                "side": side,
                "projected_pair_cost": projected_pair_cost,
                "quantity": quantity,
                "reason": reason,
                "fill_json": fill.as_dict() if fill else None,
            }
        )
