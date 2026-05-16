from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

PassiveSide = Literal["ABOVE", "BELOW"]


@dataclass(frozen=True)
class PairArbPassiveConfig:
    rebid_interval_seconds: float = 60.0
    starter_bid_price: float = 0.45
    grid_bid_discount: float = 0.05
    max_bid_price: float = 0.55
    target_pair_qty: float = 100.0
    max_imbalance_qty: float = 25.0
    max_order_age_seconds: float = 60.0
    order_qty: float = 5.0
    projected_pair_cost_threshold: float = 0.95
    stop_adding_seconds_to_expiry: float = 60.0
    rebalance_only_when_imbalanced: bool = True
    balanced_bid_discount: float = 0.05
    imbalance_bid_discount: float = 0.02
    max_same_side_fills_without_pair: int = 3


@dataclass(frozen=True)
class PassiveOrder:
    id: str
    event_key: str
    market_ticker: str
    side: PassiveSide
    bid_price: float
    quantity: float
    created_ts: datetime
    expires_ts: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_key": self.event_key,
            "market_ticker": self.market_ticker,
            "side": self.side,
            "bid_price": self.bid_price,
            "quantity": self.quantity,
            "created_ts": self.created_ts.isoformat(),
            "expires_ts": self.expires_ts.isoformat(),
        }


@dataclass(frozen=True)
class PassiveFill:
    order_id: str
    event_key: str
    market_ticker: str
    side: PassiveSide
    price: float
    quantity: float
    ts: datetime

    @property
    def cost(self) -> float:
        return round(self.price * self.quantity, 10)

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "event_key": self.event_key,
            "market_ticker": self.market_ticker,
            "side": self.side,
            "price": self.price,
            "quantity": self.quantity,
            "cost": self.cost,
            "ts": self.ts.isoformat(),
        }


@dataclass(frozen=True)
class PassiveDecision:
    orders_placed: int
    fills: int
    reason: str
    orders: list[PassiveOrder] = field(default_factory=list)
    fill_events: list[PassiveFill] = field(default_factory=list)


@dataclass
class PassivePosition:
    event_key: str
    market_ticker: str
    market_close_time: str
    strike: float
    first_seen_ts: datetime | None = None
    first_pair_ts: datetime | None = None
    held_above_qty: float = 0.0
    held_below_qty: float = 0.0
    above_cost: float = 0.0
    below_cost: float = 0.0
    last_seconds_to_close: float = 0.0
    last_distance_from_strike: float = 0.0
    largest_inventory_imbalance: float = 0.0
    same_side_fill_streak: int = 0
    _last_fill_side: PassiveSide | None = None

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
    def time_to_first_pair(self) -> float | None:
        if self.first_seen_ts is None or self.first_pair_ts is None:
            return None
        return (self.first_pair_ts - self.first_seen_ts).total_seconds()

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
    def max_capital_at_risk(self) -> float:
        return self.total_cost

    @property
    def locked_profit_to_max_capital_at_risk(self) -> float:
        return round(self.locked_profit / self.max_capital_at_risk, 10) if self.max_capital_at_risk > 0 else 0.0

    def add_fill(self, fill: PassiveFill) -> None:
        had_pair = self.paired_qty > 0
        if fill.side == "ABOVE":
            self.held_above_qty += fill.quantity
            self.above_cost += fill.cost
        else:
            self.held_below_qty += fill.quantity
            self.below_cost += fill.cost

        if self._last_fill_side == fill.side:
            self.same_side_fill_streak += 1
        else:
            self.same_side_fill_streak = 1
        self._last_fill_side = fill.side
        self.largest_inventory_imbalance = max(self.largest_inventory_imbalance, self.unpaired_directional_exposure)
        if not had_pair and self.paired_qty > 0:
            self.first_pair_ts = fill.ts
            self.same_side_fill_streak = 0
            self._last_fill_side = None

    def projected_pair_cost_for(self, side: PassiveSide, price: float, quantity: float) -> float:
        above_qty = self.held_above_qty + (quantity if side == "ABOVE" else 0.0)
        below_qty = self.held_below_qty + (quantity if side == "BELOW" else 0.0)
        above_cost = self.above_cost + (price * quantity if side == "ABOVE" else 0.0)
        below_cost = self.below_cost + (price * quantity if side == "BELOW" else 0.0)
        if above_qty and below_qty:
            return round(above_cost / above_qty + below_cost / below_qty, 10)
        if not self.held_above_qty and not self.held_below_qty:
            if price >= 0.55:
                return price * 2
            return price + 0.35
        existing_avg = self.held_above_avg if self.held_above_qty else self.held_below_avg
        return round(existing_avg + price, 10)

    def projected_imbalance_for(self, side: PassiveSide, quantity: float) -> float:
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
            "time_to_first_pair": self.time_to_first_pair,
            "largest_inventory_imbalance": self.largest_inventory_imbalance,
            "same_side_fill_streak": self.same_side_fill_streak,
        }


class PairArbPassiveManager:
    def __init__(self, config: PairArbPassiveConfig | None = None) -> None:
        self.config = config or PairArbPassiveConfig()
        self.positions: dict[str, PassivePosition] = {}
        self.open_orders: list[PassiveOrder] = []
        self.orders: list[PassiveOrder] = []
        self.fills: list[PassiveFill] = []
        self.events: list[dict[str, Any]] = []
        self.last_rebid_ts: dict[str, datetime] = {}
        self._order_seq = 0

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
    ) -> PassiveDecision:
        position = self.positions.setdefault(
            event_key,
            PassivePosition(
                event_key=event_key,
                market_ticker=market_ticker,
                market_close_time=market_close_time,
                strike=strike,
                first_seen_ts=ts,
            ),
        )
        position.last_seconds_to_close = seconds_to_close
        position.last_distance_from_strike = distance_from_strike
        fills = self._fill_touched_orders(event_key=event_key, ts=ts, above_ask=above_ask, below_ask=below_ask, position=position)
        self._expire_orders(ts)
        if seconds_to_close <= self.config.stop_adding_seconds_to_expiry:
            return PassiveDecision(0, len(fills), "stop_adding_near_expiry", fill_events=fills)
        if not self._rebid_due(event_key, ts):
            return PassiveDecision(0, len(fills), "rebid_interval", fill_events=fills)
        orders, reason = self._place_orders(position=position, ts=ts, above_ask=above_ask, below_ask=below_ask)
        if orders:
            self.last_rebid_ts[event_key] = ts
        return PassiveDecision(len(orders), len(fills), reason, orders=orders, fill_events=fills)

    def _fill_touched_orders(self, *, event_key: str, ts: datetime, above_ask: float | None, below_ask: float | None, position: PassivePosition) -> list[PassiveFill]:
        fills: list[PassiveFill] = []
        remaining: list[PassiveOrder] = []
        for order in self.open_orders:
            if order.event_key != event_key:
                remaining.append(order)
                continue
            ask = above_ask if order.side == "ABOVE" else below_ask
            if ask is not None and ask <= order.bid_price:
                fill = PassiveFill(order.id, order.event_key, order.market_ticker, order.side, order.bid_price, order.quantity, ts)
                position.add_fill(fill)
                fills.append(fill)
                self.fills.append(fill)
                self.events.append({"type": "fill", **fill.as_dict()})
            else:
                remaining.append(order)
        self.open_orders = remaining
        return fills

    def _expire_orders(self, ts: datetime) -> None:
        self.open_orders = [
            order
            for order in self.open_orders
            if order.expires_ts > ts
            or (order.event_key in self.positions and (self.positions[order.event_key].held_above_qty + self.positions[order.event_key].held_below_qty) <= 0)
        ]

    def _rebid_due(self, event_key: str, ts: datetime) -> bool:
        last = self.last_rebid_ts.get(event_key)
        return last is None or (ts - last).total_seconds() >= self.config.rebid_interval_seconds

    def _candidate_sides(self, position: PassivePosition) -> list[PassiveSide]:
        if not self.config.rebalance_only_when_imbalanced or position.held_above_qty == position.held_below_qty:
            return ["ABOVE", "BELOW"]
        if position.held_above_qty > position.held_below_qty:
            return ["BELOW"]
        return ["ABOVE"]

    def _bid_discount_for(self, position: PassivePosition) -> float:
        if position.held_above_qty == position.held_below_qty:
            return self.config.balanced_bid_discount
        return self.config.imbalance_bid_discount

    def _open_qty(self, event_key: str, side: PassiveSide) -> float:
        return sum(order.quantity for order in self.open_orders if order.event_key == event_key and order.side == side)

    def _would_exceed_same_side_fill_streak(self, position: PassivePosition, side: PassiveSide) -> bool:
        if position.paired_qty > 0:
            return False
        if position._last_fill_side != side:
            return False
        return position.same_side_fill_streak >= self.config.max_same_side_fills_without_pair

    def _place_orders(self, *, position: PassivePosition, ts: datetime, above_ask: float | None, below_ask: float | None) -> tuple[list[PassiveOrder], str]:
        if above_ask is None or below_ask is None:
            return [], "invalid_book"
        asks: dict[PassiveSide, float] = {"ABOVE": above_ask, "BELOW": below_ask}
        discount = self._bid_discount_for(position)
        candidate_bid_by_side: dict[PassiveSide, float] = {
            "ABOVE": round(min(self.config.max_bid_price, above_ask - discount), 10),
            "BELOW": round(min(self.config.max_bid_price, below_ask - discount), 10),
        }
        candidate_pair_cost = candidate_bid_by_side["ABOVE"] + candidate_bid_by_side["BELOW"]
        placed: list[PassiveOrder] = []
        saw_rebalance_skip = False
        saw_streak_skip = False
        for side in self._candidate_sides(position):
            ask = asks[side]
            if any(order.event_key == position.event_key and order.side == side for order in self.open_orders):
                continue
            side_qty = (position.held_above_qty if side == "ABOVE" else position.held_below_qty) + self._open_qty(position.event_key, side)
            if side_qty >= self.config.target_pair_qty:
                continue
            if side_qty + self.config.order_qty > self.config.target_pair_qty:
                continue
            if self._would_exceed_same_side_fill_streak(position, side):
                saw_streak_skip = True
                self.events.append({"type": "missed_order", "event_key": position.event_key, "side": side, "reason": "max_same_side_fills_without_pair"})
                continue
            bid = candidate_bid_by_side[side]
            if bid <= 0:
                continue
            if position.projected_imbalance_for(side, self.config.order_qty) > self.config.max_imbalance_qty:
                self.events.append({"type": "missed_order", "event_key": position.event_key, "side": side, "reason": "max_imbalance_qty"})
                continue
            current_projection = position.projected_pair_cost_for(side, bid, self.config.order_qty)
            other_open = next((order for order in self.open_orders if order.event_key == position.event_key and order.side != side), None)
            if other_open is not None:
                current_projection = bid + other_open.bid_price
            elif not position.held_above_qty and not position.held_below_qty:
                current_projection = candidate_pair_cost
            if current_projection > self.config.projected_pair_cost_threshold:
                self.events.append({"type": "missed_order", "event_key": position.event_key, "side": side, "reason": "projected_pair_cost_above_threshold"})
                continue
            self._order_seq += 1
            order = PassiveOrder(
                id=f"passive-{position.event_key}-{self._order_seq}",
                event_key=position.event_key,
                market_ticker=position.market_ticker,
                side=side,
                bid_price=bid,
                quantity=self.config.order_qty,
                created_ts=ts,
                expires_ts=ts + timedelta(seconds=self.config.max_order_age_seconds),
            )
            self.open_orders.append(order)
            self.orders.append(order)
            placed.append(order)
            self.events.append({"type": "order", **order.as_dict()})

        if placed:
            return placed, "orders_placed"
        active_same_event = any(order.event_key == position.event_key for order in self.open_orders)
        if active_same_event or position.held_above_qty >= self.config.target_pair_qty or position.held_below_qty >= self.config.target_pair_qty:
            return [], "target_pair_qty_reached_or_order_exists"
        if saw_streak_skip:
            return [], "max_same_side_fills_without_pair"
        if saw_rebalance_skip:
            return [], "rebalance_only_when_imbalanced"
        if self.events and self.events[-1].get("reason") == "projected_pair_cost_above_threshold":
            return [], "projected_pair_cost_above_threshold"
        return [], "target_pair_qty_reached_or_order_exists"
