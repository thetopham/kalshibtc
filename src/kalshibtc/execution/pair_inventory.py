from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

LegSide = Literal["ABOVE", "BELOW"]


@dataclass(frozen=True)
class PairArbConfig:
    pair_cost_threshold: float = 0.95
    initial_qty: float = 25.0
    hedge_qty: float = 25.0
    max_seed_price: float = 0.65
    max_unpaired_qty: float = 25.0
    min_expected_opposite_room: float = 0.20
    max_unpaired_exposure: float = 100.0
    max_total_qty_per_event: float = 200.0
    slippage: float = 0.01
    fee_per_contract: float = 0.0
    min_seconds_to_close: float = 30.0
    max_seconds_to_close: float = 14 * 60.0
    min_abs_distance_from_strike: float = 10.0
    min_abs_slope: float = 0.5


@dataclass(frozen=True)
class PairFill:
    event_key: str
    market_ticker: str
    side: LegSide
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
class HedgeResult:
    filled: bool
    reason: str
    fill: PairFill | None = None
    pair_cost: float | None = None
    locked_profit_after: float | None = None


@dataclass(frozen=True)
class MissedHedgeOpportunity:
    event_key: str
    market_ticker: str
    held_side: LegSide | None
    opposite_side: LegSide | None
    opposite_ask: float | None
    projected_pair_cost: float | None
    threshold: float
    reason: str
    ts: datetime
    distance_from_strike: float
    seconds_to_close: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_key": self.event_key,
            "market_ticker": self.market_ticker,
            "held_side": self.held_side,
            "opposite_side": self.opposite_side,
            "opposite_ask": self.opposite_ask,
            "projected_pair_cost": self.projected_pair_cost,
            "threshold": self.threshold,
            "reason": self.reason,
            "ts": self.ts.isoformat(),
            "distance_from_strike": self.distance_from_strike,
            "seconds_to_close": self.seconds_to_close,
        }


@dataclass(frozen=True)
class RejectedSeedAttempt:
    event_key: str
    market_ticker: str
    side: LegSide
    ask_price: float
    effective_price: float
    quantity: float
    threshold: float
    opposite_ask: float | None
    required_opposite_ask: float
    reason: str
    ts: datetime
    distance_from_strike: float
    seconds_to_close: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_key": self.event_key,
            "market_ticker": self.market_ticker,
            "side": self.side,
            "ask_price": self.ask_price,
            "effective_price": self.effective_price,
            "quantity": self.quantity,
            "threshold": self.threshold,
            "opposite_ask": self.opposite_ask,
            "required_opposite_ask": self.required_opposite_ask,
            "reason": self.reason,
            "ts": self.ts.isoformat(),
            "distance_from_strike": self.distance_from_strike,
            "seconds_to_close": self.seconds_to_close,
        }


@dataclass
class PairPosition:
    event_key: str
    market_ticker: str
    market_close_time: str
    strike: float
    held_above_qty: float = 0.0
    held_below_qty: float = 0.0
    above_cost: float = 0.0
    below_cost: float = 0.0
    last_ts: datetime | None = None
    last_distance_from_strike: float = 0.0
    last_seconds_to_close: float = 0.0
    fills: list[PairFill] = field(default_factory=list)

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
    def unpaired_side(self) -> LegSide | None:
        if self.held_above_qty > self.held_below_qty:
            return "ABOVE"
        if self.held_below_qty > self.held_above_qty:
            return "BELOW"
        return None

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

    def add_fill(self, *, side: LegSide, price: float, quantity: float, fee: float, ts: datetime, reason: str) -> PairFill:
        fill = PairFill(
            event_key=self.event_key,
            market_ticker=self.market_ticker,
            side=side,
            price=price,
            quantity=quantity,
            fee=fee,
            ts=ts,
            reason=reason,
        )
        if side == "ABOVE":
            self.held_above_qty += quantity
            self.above_cost += fill.cost
        else:
            self.held_below_qty += quantity
            self.below_cost += fill.cost
        self.last_ts = ts
        self.fills.append(fill)
        return fill

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
            "unpaired_side": self.unpaired_side,
            "time_to_expiry": self.last_seconds_to_close,
            "distance_from_strike": self.last_distance_from_strike,
            "fills_json": [fill.as_dict() for fill in self.fills],
        }


class InventoryManager:
    def __init__(self, config: PairArbConfig | None = None) -> None:
        self.config = config or PairArbConfig()
        self.positions: dict[str, PairPosition] = {}
        self.missed_hedges: list[MissedHedgeOpportunity] = []
        self.rejected_seed_attempts: list[RejectedSeedAttempt] = []
        self.hedge_events: list[HedgeResult] = []

    def buy_initial_leg(
        self,
        *,
        event_key: str,
        market_ticker: str,
        side: LegSide,
        ask_price: float,
        quantity: float,
        ts: datetime,
        market_close_time: str,
        strike: float,
        distance_from_strike: float,
        seconds_to_close: float,
        opposite_ask: float | None = None,
    ) -> HedgeResult:
        effective_price = round(ask_price + self.config.slippage, 10)
        seed_rejection = self._seed_rejection_reason(effective_price=effective_price, quantity=quantity)
        if seed_rejection is not None:
            self.record_rejected_seed(
                event_key=event_key,
                market_ticker=market_ticker,
                side=side,
                ask_price=ask_price,
                effective_price=effective_price,
                quantity=quantity,
                opposite_ask=opposite_ask,
                reason=seed_rejection,
                ts=ts,
                distance_from_strike=distance_from_strike,
                seconds_to_close=seconds_to_close,
            )
            return HedgeResult(False, seed_rejection)
        position = self.positions.setdefault(
            event_key,
            PairPosition(
                event_key=event_key,
                market_ticker=market_ticker,
                market_close_time=market_close_time,
                strike=strike,
            ),
        )
        if position.held_above_qty or position.held_below_qty:
            return HedgeResult(False, "initial_inventory_already_exists")
        if quantity > self.config.max_unpaired_exposure:
            self.record_rejected_seed(
                event_key=event_key,
                market_ticker=market_ticker,
                side=side,
                ask_price=ask_price,
                effective_price=effective_price,
                quantity=quantity,
                opposite_ask=opposite_ask,
                reason="max_unpaired_qty_reached",
                ts=ts,
                distance_from_strike=distance_from_strike,
                seconds_to_close=seconds_to_close,
            )
            return HedgeResult(False, "max_unpaired_qty_reached")
        fill = position.add_fill(
            side=side,
            price=effective_price,
            quantity=quantity,
            fee=quantity * self.config.fee_per_contract,
            ts=ts,
            reason="initial_inventory_leg",
        )
        position.last_distance_from_strike = distance_from_strike
        position.last_seconds_to_close = seconds_to_close
        return HedgeResult(True, "filled_initial_inventory", fill=fill)

    def _seed_rejection_reason(self, *, effective_price: float, quantity: float) -> str | None:
        if effective_price > self.config.max_seed_price:
            return "seed_price_too_high"
        if quantity > self.config.max_unpaired_qty:
            return "max_unpaired_qty_reached"
        required_opposite_ask = self.config.pair_cost_threshold - effective_price
        if required_opposite_ask <= self.config.min_expected_opposite_room:
            return "insufficient_opposite_room"
        return None

    def record_rejected_seed(
        self,
        *,
        event_key: str,
        market_ticker: str,
        side: LegSide,
        ask_price: float,
        effective_price: float,
        quantity: float,
        opposite_ask: float | None,
        reason: str,
        ts: datetime,
        distance_from_strike: float,
        seconds_to_close: float,
    ) -> None:
        self.rejected_seed_attempts.append(
            RejectedSeedAttempt(
                event_key=event_key,
                market_ticker=market_ticker,
                side=side,
                ask_price=ask_price,
                effective_price=effective_price,
                quantity=quantity,
                threshold=self.config.max_seed_price,
                opposite_ask=opposite_ask,
                required_opposite_ask=self.config.pair_cost_threshold - effective_price,
                reason=reason,
                ts=ts,
                distance_from_strike=distance_from_strike,
                seconds_to_close=seconds_to_close,
            )
        )

    def record_missed_hedge(
        self,
        *,
        position: PairPosition | None,
        event_key: str,
        market_ticker: str,
        opposite_side: LegSide | None,
        opposite_ask: float | None,
        projected_pair_cost: float | None,
        reason: str,
        ts: datetime,
        distance_from_strike: float,
        seconds_to_close: float,
    ) -> None:
        self.missed_hedges.append(
            MissedHedgeOpportunity(
                event_key=event_key,
                market_ticker=market_ticker,
                held_side=position.unpaired_side if position else None,
                opposite_side=opposite_side,
                opposite_ask=opposite_ask,
                projected_pair_cost=projected_pair_cost,
                threshold=self.config.pair_cost_threshold,
                reason=reason,
                ts=ts,
                distance_from_strike=distance_from_strike,
                seconds_to_close=seconds_to_close,
            )
        )


class HedgeManager:
    def __init__(self, config: PairArbConfig | None = None) -> None:
        self.config = config or PairArbConfig()

    def evaluate_and_fill(
        self,
        *,
        inventory: InventoryManager,
        event_key: str,
        opposite_ask: float | None,
        ts: datetime,
        distance_from_strike: float,
        seconds_to_close: float,
    ) -> HedgeResult:
        position = inventory.positions.get(event_key)
        if position is None:
            return HedgeResult(False, "no_inventory")
        position.last_distance_from_strike = distance_from_strike
        position.last_seconds_to_close = seconds_to_close
        held_side = position.unpaired_side
        if held_side is None:
            result = HedgeResult(False, "already_fully_paired")
            inventory.hedge_events.append(result)
            return result
        opposite_side: LegSide = "BELOW" if held_side == "ABOVE" else "ABOVE"
        if opposite_ask is None or opposite_ask <= 0 or opposite_ask > 1:
            inventory.record_missed_hedge(
                position=position,
                event_key=event_key,
                market_ticker=position.market_ticker,
                opposite_side=opposite_side,
                opposite_ask=opposite_ask,
                projected_pair_cost=None,
                reason="invalid_opposite_ask",
                ts=ts,
                distance_from_strike=distance_from_strike,
                seconds_to_close=seconds_to_close,
            )
            return HedgeResult(False, "invalid_opposite_ask")
        projected_price = round(opposite_ask + self.config.slippage, 10)
        projected_pair_cost = projected_price + (position.held_above_avg if held_side == "ABOVE" else position.held_below_avg)
        if projected_pair_cost > self.config.pair_cost_threshold:
            inventory.record_missed_hedge(
                position=position,
                event_key=event_key,
                market_ticker=position.market_ticker,
                opposite_side=opposite_side,
                opposite_ask=opposite_ask,
                projected_pair_cost=projected_pair_cost,
                reason="pair_cost_above_threshold",
                ts=ts,
                distance_from_strike=distance_from_strike,
                seconds_to_close=seconds_to_close,
            )
            return HedgeResult(False, "pair_cost_above_threshold", pair_cost=projected_pair_cost)
        quantity = min(self.config.hedge_qty, position.unpaired_directional_exposure)
        total_after = position.held_above_qty + position.held_below_qty + quantity
        if quantity <= 0 or total_after > self.config.max_total_qty_per_event:
            inventory.record_missed_hedge(
                position=position,
                event_key=event_key,
                market_ticker=position.market_ticker,
                opposite_side=opposite_side,
                opposite_ask=opposite_ask,
                projected_pair_cost=projected_pair_cost,
                reason="max_total_qty_per_event",
                ts=ts,
                distance_from_strike=distance_from_strike,
                seconds_to_close=seconds_to_close,
            )
            return HedgeResult(False, "max_total_qty_per_event", pair_cost=projected_pair_cost)
        fill = position.add_fill(
            side=opposite_side,
            price=projected_price,
            quantity=quantity,
            fee=quantity * self.config.fee_per_contract,
            ts=ts,
            reason="paired_leg_hedge",
        )
        result = HedgeResult(
            True,
            "filled_pair_hedge",
            fill=fill,
            pair_cost=position.pair_cost,
            locked_profit_after=position.locked_profit,
        )
        inventory.hedge_events.append(result)
        return result
