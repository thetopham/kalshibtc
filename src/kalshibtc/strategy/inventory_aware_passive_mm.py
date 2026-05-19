from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal, SupportsFloat

from ..execution.paper import PaperFill
from ..market.state import MarketState
from ..probability.models import distance_probability
from .signals import Signal

Side = Literal["yes", "no"]


@dataclass(frozen=True)
class InventoryAwarePassiveMMConfig:
    """Replay-first Polymarket BTC 15m passive inventory MM knobs.

    This strategy is intentionally an emulator only. It emits passive quote intents
    through the existing replay/risk seam. Use `--fill-timing next-tick` so a quote
    must exist before a later snapshot touches its limit price.
    """

    name: str = "inventory_aware_passive_mm"
    target_pair_cost: float = 0.95
    hard_pair_cost_ceiling: float = 0.99
    absolute_pair_cost_ceiling: float = 1.00
    max_weak_to_strong_ratio: float = 1.0
    max_unpaired_strong_qty: float = 300.0
    max_unpaired_weak_qty: float = 0.0
    max_total_contracts: float = 2500.0
    max_pair_notional: float = 2000.0
    max_unpaired_notional: float = 300.0
    max_fill_count_per_market: int = 150
    min_visible_depth: float = 25.0
    stale_book_seconds: float = 2.0
    repair_only_seconds_before_close: float = 180.0
    stop_new_seed_seconds_before_close: float = 300.0
    open_ladder_end_seconds_after_open: float = 180.0
    open_ladder_levels: tuple[tuple[float, float], ...] = (
        (0.02, 25.0),
        (0.05, 50.0),
        (0.10, 75.0),
        (0.15, 100.0),
    )
    repair_ladder_levels: tuple[tuple[float, float], ...] = (
        (0.30, 50.0),
        (0.20, 100.0),
        (0.12, 150.0),
        (0.06, 200.0),
    )
    tick_size: float = 0.01
    z_strong_threshold: float = 0.5
    near_strike_z_threshold: float = 0.5
    extreme_z_threshold: float = 2.0
    fallback_sigma_per_sqrt_second: float = 35.0
    min_seconds_between_quote_updates: float = 0.0
    quote_ttl_seconds: float = 30.0
    min_order_contracts: float = 5.0
    min_confidence: float = 0.70


@dataclass
class InventoryLot:
    qty: float = 0.0
    cost: float = 0.0

    @property
    def avg_price(self) -> float | None:
        return self.cost / self.qty if self.qty > 0 else None

    def add(self, *, qty: float, price: float) -> None:
        self.qty += qty
        self.cost += qty * price


@dataclass
class InventoryState:
    yes: InventoryLot = field(default_factory=InventoryLot)
    no: InventoryLot = field(default_factory=InventoryLot)
    max_yes_qty: float = 0.0
    max_no_qty: float = 0.0

    def copy(self) -> InventoryState:
        return InventoryState(
            yes=InventoryLot(self.yes.qty, self.yes.cost),
            no=InventoryLot(self.no.qty, self.no.cost),
            max_yes_qty=self.max_yes_qty,
            max_no_qty=self.max_no_qty,
        )

    def apply_buy(self, *, side: Side, price: float, qty: float) -> None:
        if side == "yes":
            self.yes.add(qty=qty, price=price)
            self.max_yes_qty = max(self.max_yes_qty, self.yes.qty)
        else:
            self.no.add(qty=qty, price=price)
            self.max_no_qty = max(self.max_no_qty, self.no.qty)

    def project_buy(self, *, side: Side, price: float, qty: float) -> InventoryState:
        projected = self.copy()
        projected.apply_buy(side=side, price=price, qty=qty)
        return projected

    @property
    def yes_avg(self) -> float | None:
        return self.yes.avg_price

    @property
    def no_avg(self) -> float | None:
        return self.no.avg_price

    @property
    def matched_qty(self) -> float:
        return min(self.yes.qty, self.no.qty)

    @property
    def matched_pair_cost(self) -> float | None:
        if self.yes_avg is None or self.no_avg is None:
            return None
        return self.yes_avg + self.no_avg

    @property
    def locked_edge_if_held(self) -> float | None:
        pair_cost = self.matched_pair_cost
        if pair_cost is None:
            return None
        return self.matched_qty * (1.0 - pair_cost)

    @property
    def unpaired_side(self) -> Side | None:
        if self.yes.qty > self.no.qty:
            return "yes"
        if self.no.qty > self.yes.qty:
            return "no"
        return None

    @property
    def unpaired_qty(self) -> float:
        return abs(self.yes.qty - self.no.qty)

    @property
    def total_contracts(self) -> float:
        return self.yes.qty + self.no.qty

    @property
    def total_notional(self) -> float:
        return self.yes.cost + self.no.cost

    def unpaired_notional(self) -> float:
        side = self.unpaired_side
        if side == "yes" and self.yes_avg is not None:
            return self.unpaired_qty * self.yes_avg
        if side == "no" and self.no_avg is not None:
            return self.unpaired_qty * self.no_avg
        return 0.0

    def to_snapshot(self) -> dict[str, float | str | None]:
        return {
            "yes_qty": self.yes.qty,
            "yes_cost": self.yes.cost,
            "yes_avg": self.yes_avg,
            "no_qty": self.no.qty,
            "no_cost": self.no.cost,
            "no_avg": self.no_avg,
            "matched_qty": self.matched_qty,
            "matched_pair_cost": self.matched_pair_cost,
            "locked_edge_if_held": self.locked_edge_if_held,
            "unpaired_side": self.unpaired_side,
            "unpaired_qty": self.unpaired_qty,
            "unpaired_notional": self.unpaired_notional(),
            "total_contracts": self.total_contracts,
            "total_notional": self.total_notional,
            "max_yes_qty": self.max_yes_qty,
            "max_no_qty": self.max_no_qty,
        }


@dataclass(frozen=True)
class QuoteIntent:
    side: Side
    limit_price: float
    contracts: float
    reason: str
    repair_fill: bool
    projected_pair_cost: float | None
    projected_matched_qty: float
    projected_unpaired_side: Side | None
    projected_unpaired_qty: float
    allowed: bool
    blocked_reason: str | None
    tags: tuple[str, ...]


@dataclass(frozen=True)
class InventoryAwareSnapshot:
    ts: str
    market_ticker: str
    yes_qty: float
    no_qty: float
    yes_cost: float
    no_cost: float
    total_notional: float
    matched_qty: float
    matched_pair_cost: float | None
    locked_edge_if_held: float | None
    unpaired_side: str | None
    unpaired_qty: float
    unpaired_notional: float
    strong_side: str | None
    weak_side: str | None
    z_score: float | None
    probability_yes: float | None
    probability_no: float | None
    seconds_to_close: float


@dataclass
class _RestingQuote:
    side: Side
    limit_price: float
    contracts: float
    reason: str
    placed_ts: datetime
    key: str


@dataclass
class InventoryAwarePassiveMMStrategy:
    """Inventory-aware passive MM emulator for BTC Up/Down replay.

    The strategy emits one passive quote intent per tick. Pair construction and
    repair fills are gated by projected YES_avg + NO_avg, weak<=strong balance,
    notional/inventory caps, visible depth, and stale-book checks. It never sends
    real orders; the replay engine converts allowed signals into simulated paper
    fills.
    """

    config: InventoryAwarePassiveMMConfig = field(default_factory=InventoryAwarePassiveMMConfig)

    def __post_init__(self) -> None:
        self._active_market_ticker: str | None = None
        self._inventory = InventoryState()
        self._resting_quotes: dict[str, _RestingQuote] = {}
        self._filled_quote_keys: set[str] = set()
        self._last_quote_ts: datetime | None = None
        self._fill_count = 0
        self.position_history: list[InventoryAwareSnapshot] = []
        self.decision_history: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return self.config.name

    def on_tick(self, state: MarketState) -> Signal:
        if self._active_market_ticker != state.contract.ticker:
            self._reset(state.contract.ticker)
        self._expire_quotes(state.tick.ts)
        model = self._side_model(state)
        intent = self._choose_intent(state, model=model)
        self._record_snapshot(state, model=model)
        signal = self._signal_from_intent(state, intent, model=model)
        self._record_decision(state, signal, intent=intent, model=model)
        return signal

    def on_fill(self, state: MarketState, fill: PaperFill) -> None:
        side = _signal_side_to_side(fill.side)
        if side is None:
            return
        order_key = self._quote_key_for_fill(fill)
        allowed_contracts = self._contracts_allowed_after_reprice(side=side, price=fill.entry_price, requested=fill.contracts)
        if allowed_contracts <= 0:
            self._remove_quote(order_key)
            self._last_quote_ts = state.tick.ts
            self._record_snapshot(state, model=self._side_model(state))
            return
        adjusted = fill if allowed_contracts >= fill.contracts else replace(
            fill,
            contracts=allowed_contracts,
            notional=round(allowed_contracts * fill.entry_price, 6),
        )
        self._inventory.apply_buy(side=side, price=adjusted.entry_price, qty=adjusted.contracts)
        self._fill_count += 1
        self._remove_quote(order_key)
        self._last_quote_ts = state.tick.ts
        self._record_snapshot(state, model=self._side_model(state))

    def _reset(self, market_ticker: str) -> None:
        self._active_market_ticker = market_ticker
        self._inventory = InventoryState()
        self._resting_quotes = {}
        self._filled_quote_keys = set()
        self._last_quote_ts = None
        self._fill_count = 0

    def _choose_intent(self, state: MarketState, *, model: dict[str, Any]) -> QuoteIntent | None:
        if self._last_quote_ts is not None:
            elapsed = (state.tick.ts - self._last_quote_ts).total_seconds()
            if elapsed < self.config.min_seconds_between_quote_updates:
                return None
        if self._fill_count >= self.config.max_fill_count_per_market:
            return self._blocked_intent("yes", "max fill count per market", state, model=model)
        if state.seconds_to_close <= 0:
            return self._blocked_intent("yes", "expired market", state, model=model)
        if self._book_age_seconds(state) > self.config.stale_book_seconds:
            return self._blocked_intent("yes", "stale_book", state, model=model)

        repair = self._repair_intents(state, model=model)
        for intent in repair:
            if intent.allowed:
                return intent
        if state.seconds_to_close <= self.config.repair_only_seconds_before_close:
            return repair[0] if repair else self._blocked_intent("yes", "repair-only window no imbalance", state, model=model)
        if state.seconds_to_close <= self.config.stop_new_seed_seconds_before_close and self._inventory.total_contracts <= 0:
            return self._blocked_intent("yes", "new seed stopped near close", state, model=model)

        for intent in self._open_ladder_intents(state, model=model):
            if intent.allowed and self._quote_is_new(intent):
                return intent
        for intent in self._mid_ladder_intents(state, model=model):
            if intent.allowed and self._quote_is_new(intent):
                return intent
        candidates = repair + self._open_ladder_intents(state, model=model) + self._mid_ladder_intents(state, model=model)
        return candidates[0] if candidates else self._blocked_intent("yes", "no quote candidates", state, model=model)

    def _open_ladder_intents(self, state: MarketState, *, model: dict[str, Any]) -> list[QuoteIntent]:
        if not self._in_open_ladder_window(state):
            return []
        intents: list[QuoteIntent] = []
        for side in ("yes", "no"):
            mid = self._side_mid(state, side)
            if mid is None:
                continue
            for offset, size in self.config.open_ladder_levels:
                limit_price = self._round_price(max(self.config.tick_size, mid - offset))
                intents.append(self._build_intent(side=side, limit_price=limit_price, contracts=size, reason="opening passive ladder", repair_fill=False, state=state, model=model))
        return intents

    def _mid_ladder_intents(self, state: MarketState, *, model: dict[str, Any]) -> list[QuoteIntent]:
        weak_side = model.get("weak_side")
        strong_side = model.get("strong_side")
        ordered_sides: list[Side] = []
        if weak_side in {"yes", "no"}:
            ordered_sides.append(weak_side)
        if strong_side in {"yes", "no"}:
            ordered_sides.append(strong_side)
        for side in ("yes", "no"):
            if side not in ordered_sides:
                ordered_sides.append(side)

        intents: list[QuoteIntent] = []
        for side in ordered_sides:
            bid = self._side_bid(state, side)
            ask = self._side_ask(state, side)
            if bid is None or ask is None:
                continue
            max_price = self._max_acceptable_bid(side=side)
            passive_price = min(max(bid + self.config.tick_size, self.config.tick_size), ask - self.config.tick_size, max_price)
            passive_price = self._round_price(passive_price)
            if passive_price <= 0:
                continue
            size = self._default_quote_size(side=side, model=model)
            reason = "weak-side passive repair quote" if side == weak_side else "capped strong-side passive quote"
            intents.append(self._build_intent(side=side, limit_price=passive_price, contracts=size, reason=reason, repair_fill=False, state=state, model=model))
        return intents

    def _repair_intents(self, state: MarketState, *, model: dict[str, Any]) -> list[QuoteIntent]:
        if self._inventory.unpaired_side is None:
            return []
        side = _opposite(self._inventory.unpaired_side)
        if side is None:
            return []
        intents: list[QuoteIntent] = []
        needed = self._inventory.unpaired_qty
        for max_price, size in self.config.repair_ladder_levels:
            contracts = min(size, needed)
            intents.append(self._build_intent(side=side, limit_price=max_price, contracts=contracts, reason="inventory repair ladder", repair_fill=True, state=state, model=model))
        return intents

    def _build_intent(
        self,
        *,
        side: Side,
        limit_price: float,
        contracts: float,
        reason: str,
        repair_fill: bool,
        state: MarketState,
        model: dict[str, Any],
    ) -> QuoteIntent:
        contracts = max(0.0, min(float(contracts), self._remaining_contract_capacity()))
        visible_depth = self._visible_ask_depth(side=side, limit_price=limit_price, state=state)
        # A passive bid below the current ask is a valid resting quote even when
        # current ask-side depth at-or-below the bid is zero.  Depth should cap
        # size only when the quote is already touched/crossed on this snapshot;
        # next-tick replay then revalidates the limit against a later snapshot.
        if visible_depth is not None and visible_depth > 0:
            contracts = min(contracts, visible_depth)
        projected = self._inventory.project_buy(side=side, price=limit_price, qty=contracts) if contracts > 0 else self._inventory.copy()
        tags: list[str] = []
        blocked: str | None = None
        if contracts < self.config.min_order_contracts:
            blocked = "below_min_order_contracts"
        elif visible_depth is not None and 0 < visible_depth < self.config.min_visible_depth:
            blocked = "insufficient_depth"
        else:
            blocked = self._balance_block_reason(side=side, price=limit_price, contracts=contracts, repair_fill=repair_fill, projected=projected, model=model)
        allowed = blocked is None
        if allowed:
            pair_cost = projected.matched_pair_cost
            if pair_cost is not None and pair_cost <= self.config.target_pair_cost:
                tags.append("target_pair_cost_ok")
            elif repair_fill:
                tags.append("repair_under_hard_ceiling")
            elif side == model.get("strong_side"):
                tags.append("directional_add_capped")
        return QuoteIntent(
            side=side,
            limit_price=limit_price,
            contracts=contracts,
            reason=reason,
            repair_fill=repair_fill,
            projected_pair_cost=projected.matched_pair_cost,
            projected_matched_qty=projected.matched_qty,
            projected_unpaired_side=projected.unpaired_side,
            projected_unpaired_qty=projected.unpaired_qty,
            allowed=allowed,
            blocked_reason=blocked,
            tags=tuple(tags),
        )

    def _balance_block_reason(
        self,
        *,
        side: Side,
        price: float,
        contracts: float,
        repair_fill: bool,
        projected: InventoryState,
        model: dict[str, Any],
    ) -> str | None:
        if contracts <= 0:
            return "zero_contracts"
        if projected.total_contracts > self.config.max_total_contracts:
            return "max_total_contracts"
        if projected.total_notional > self.config.max_pair_notional + self.config.max_unpaired_notional:
            return "max_total_notional"
        pair_cost_after = projected.matched_pair_cost
        if pair_cost_after is not None and pair_cost_after > self.config.absolute_pair_cost_ceiling:
            return "pair_cost_above_absolute_ceiling"
        strong_side = model.get("strong_side")
        weak_side = model.get("weak_side")
        if weak_side in {"yes", "no"} and strong_side in {"yes", "no"}:
            weak_qty = projected.yes.qty if weak_side == "yes" else projected.no.qty
            strong_qty = projected.yes.qty if strong_side == "yes" else projected.no.qty
            max_weak = strong_qty * self.config.max_weak_to_strong_ratio
            if weak_qty > max_weak + 1e-9:
                return "weak_exceeds_strong"
        unpaired_side = projected.unpaired_side
        if unpaired_side is not None:
            if unpaired_side == strong_side and projected.unpaired_qty > self.config.max_unpaired_strong_qty:
                return "max_unpaired_strong_qty"
            if unpaired_side == weak_side and projected.unpaired_qty > self.config.max_unpaired_weak_qty:
                return "max_unpaired_weak_qty"
            if projected.unpaired_notional() > self.config.max_unpaired_notional:
                return "max_unpaired_notional"
        if pair_cost_after is not None and pair_cost_after <= self.config.target_pair_cost:
            return None
        if repair_fill and pair_cost_after is not None and pair_cost_after <= self.config.hard_pair_cost_ceiling:
            return None
        if side == strong_side:
            if pair_cost_after is None or pair_cost_after <= self.config.hard_pair_cost_ceiling:
                return None
            if self._inventory.matched_pair_cost is None and price <= self.config.hard_pair_cost_ceiling:
                return None
        return "pair_cost_above_ceiling"

    def _signal_from_intent(self, state: MarketState, intent: QuoteIntent | None, *, model: dict[str, Any]) -> Signal:
        if intent is None:
            return Signal("none", "no quote intent", 0.0, strategy=self.name, allow_price_strike_mismatch=True)
        if not intent.allowed:
            return Signal("none", intent.blocked_reason or "blocked", 0.0, strategy=self.name, features=self._features(state, intent=intent, model=model), allow_price_strike_mismatch=True)
        key = self._quote_key(intent)
        if key in self._resting_quotes:
            return Signal("none", "resting passive quote already active", 0.0, strategy=self.name, features=self._features(state, intent=intent, model=model), allow_price_strike_mismatch=True)
        if key in self._filled_quote_keys:
            return Signal("none", "resting passive quote already filled", 0.0, strategy=self.name, features=self._features(state, intent=intent, model=model), allow_price_strike_mismatch=True)
        self._resting_quotes[key] = _RestingQuote(side=intent.side, limit_price=intent.limit_price, contracts=intent.contracts, reason=intent.reason, placed_ts=state.tick.ts, key=key)
        self._last_quote_ts = state.tick.ts
        return Signal(
            side="long_above" if intent.side == "yes" else "long_below",
            reason=intent.reason,
            confidence=self.config.min_confidence,
            strategy=self.name,
            target_notional=round(intent.contracts * intent.limit_price, 6),
            estimated_shares=intent.contracts,
            features=self._features(state, intent=intent, model=model) | {"resting_order_key": key, "limit_price": intent.limit_price},
            allow_price_strike_mismatch=True,
        )

    def _side_model(self, state: MarketState) -> dict[str, Any]:
        d = state.price - state.strike
        t = max(0.0, state.seconds_to_close)
        sigma = self._sigma_per_sqrt_second(state)
        z = None
        probability_yes = None
        probability_no = None
        if sigma is not None and sigma > 0 and t > 0:
            probability = distance_probability(
                distance_to_strike=d,
                sigma_per_sqrt_second=sigma,
                seconds_to_expiry=t,
            )
            z = probability.z_score
            probability_yes = probability.probability_yes
            probability_no = probability.probability_no
        yes_mid = state.orderbook.yes_mid
        no_mid = state.orderbook.no_mid
        strong_side: Side | None = None
        if z is not None and z > self.config.z_strong_threshold:
            strong_side = "yes"
        elif z is not None and z < -self.config.z_strong_threshold:
            strong_side = "no"
        elif yes_mid is not None and no_mid is not None:
            strong_side = "yes" if yes_mid >= no_mid else "no"
        weak_side = _opposite(strong_side) if strong_side is not None else None
        regime = "unknown"
        if z is not None:
            if abs(z) < self.config.near_strike_z_threshold:
                regime = "near_strike"
            elif abs(z) > self.config.extreme_z_threshold and t < self.config.repair_only_seconds_before_close:
                regime = "likely_resolved_late"
            else:
                regime = "directional"
        return {
            "d": d,
            "t": t,
            "sigma": sigma,
            "z": z,
            "probability_yes": probability_yes,
            "probability_no": probability_no,
            "strong_side": strong_side,
            "weak_side": weak_side,
            "regime": regime,
        }

    def _sigma_per_sqrt_second(self, state: MarketState) -> float | None:
        raw = state.tick.raw | state.orderbook.raw
        for key in ("sigma_per_sqrt_second", "realized_volatility_btc_60s", "btc_sigma_60s", "atr_60s"):
            value = raw.get(key)
            if value is not None:
                try:
                    sigma = abs(float(value))
                    return sigma if sigma > 0 else None
                except (TypeError, ValueError):
                    pass
        return self.config.fallback_sigma_per_sqrt_second

    def _contracts_allowed_after_reprice(self, *, side: Side, price: float, requested: float) -> float:
        requested = max(0.0, requested)
        if requested <= 0:
            return 0.0
        projected = self._inventory.project_buy(side=side, price=price, qty=requested)
        model = {"strong_side": self._infer_strong_side_from_inventory(side), "weak_side": _opposite(self._infer_strong_side_from_inventory(side))}
        if self._balance_block_reason(side=side, price=price, contracts=requested, repair_fill=True, projected=projected, model=model) is None:
            return requested
        # Conservative fallback: if full fill violates caps, only allow remaining gross capacity.
        return min(requested, self._remaining_contract_capacity())

    def _infer_strong_side_from_inventory(self, side: Side) -> Side:
        if self._inventory.yes.qty > self._inventory.no.qty:
            return "yes"
        if self._inventory.no.qty > self._inventory.yes.qty:
            return "no"
        return side

    def _in_open_ladder_window(self, state: MarketState) -> bool:
        if state.contract.open_time is None:
            return state.seconds_to_close >= 900.0 - self.config.open_ladder_end_seconds_after_open
        seconds_after_open = (state.tick.ts - state.contract.open_time).total_seconds()
        return 0.0 <= seconds_after_open <= self.config.open_ladder_end_seconds_after_open

    def _max_acceptable_bid(self, *, side: Side) -> float:
        other_avg = self._inventory.no_avg if side == "yes" else self._inventory.yes_avg
        if other_avg is None:
            return self.config.hard_pair_cost_ceiling
        return max(self.config.tick_size, self.config.target_pair_cost - other_avg)

    def _default_quote_size(self, *, side: Side, model: dict[str, Any]) -> float:
        if side == model.get("weak_side"):
            return 50.0
        return 25.0

    def _remaining_contract_capacity(self) -> float:
        return max(0.0, self.config.max_total_contracts - self._inventory.total_contracts)

    def _quote_is_new(self, intent: QuoteIntent) -> bool:
        key = self._quote_key(intent)
        return key not in self._resting_quotes and key not in self._filled_quote_keys

    def _quote_key(self, intent: QuoteIntent) -> str:
        return f"{intent.side}:{intent.reason}:{intent.limit_price:.4f}"

    def _quote_key_for_fill(self, fill: PaperFill) -> str | None:
        side = _signal_side_to_side(fill.side)
        if side is None:
            return None
        candidates = [key for key, quote in self._resting_quotes.items() if quote.side == side and fill.entry_price <= quote.limit_price + 1e-9]
        return candidates[0] if len(candidates) == 1 else None

    def _remove_quote(self, key: str | None) -> None:
        if key is None:
            return
        self._resting_quotes.pop(key, None)
        self._filled_quote_keys.add(key)

    def _expire_quotes(self, now: datetime) -> None:
        expired = [key for key, quote in self._resting_quotes.items() if (now - quote.placed_ts).total_seconds() > self.config.quote_ttl_seconds]
        for key in expired:
            self._resting_quotes.pop(key, None)

    def _book_age_seconds(self, state: MarketState) -> float:
        return abs((state.tick.ts - state.orderbook.ts).total_seconds())

    def _side_mid(self, state: MarketState, side: Side) -> float | None:
        return state.orderbook.yes_mid if side == "yes" else state.orderbook.no_mid

    def _side_bid(self, state: MarketState, side: Side) -> float | None:
        return state.orderbook.yes_bid if side == "yes" else state.orderbook.no_bid

    def _side_ask(self, state: MarketState, side: Side) -> float | None:
        return state.orderbook.yes_ask if side == "yes" else state.orderbook.no_ask

    def _visible_ask_depth(self, *, side: Side, limit_price: float, state: MarketState) -> float | None:
        raw = state.orderbook.raw or {}
        book = raw.get(f"{side}_orderbook") or raw.get("orderbook", {}).get(side)
        if book is None:
            raw_book = raw.get("raw_book") or raw.get("raw_book_json")
            if isinstance(raw_book, str):
                try:
                    raw_book = json.loads(raw_book)
                except json.JSONDecodeError:
                    raw_book = None
            if isinstance(raw_book, dict):
                book = raw_book.get(side)
        levels = _ask_levels(book)
        if not levels:
            return None
        return sum(size for level_price, size in levels if level_price <= limit_price + 1e-9)

    def _blocked_intent(self, side: Side, reason: str, state: MarketState, *, model: dict[str, Any]) -> QuoteIntent:
        _ = state
        projected = self._inventory.copy()
        return QuoteIntent(side=side, limit_price=0.0, contracts=0.0, reason=reason, repair_fill=False, projected_pair_cost=projected.matched_pair_cost, projected_matched_qty=projected.matched_qty, projected_unpaired_side=projected.unpaired_side, projected_unpaired_qty=projected.unpaired_qty, allowed=False, blocked_reason=reason, tags=())

    def _features(self, state: MarketState, *, intent: QuoteIntent, model: dict[str, Any]) -> dict[str, float | bool | str | None]:
        snapshot = self._inventory.to_snapshot()
        return {
            **snapshot,
            "limit_price": intent.limit_price,
            "target_contracts": intent.contracts,
            "projected_pair_cost": intent.projected_pair_cost,
            "projected_matched_qty": intent.projected_matched_qty,
            "projected_unpaired_side": intent.projected_unpaired_side,
            "projected_unpaired_qty": intent.projected_unpaired_qty,
            "repair_fill": intent.repair_fill,
            "blocked_reason": intent.blocked_reason,
            "decision_tags": ",".join(intent.tags),
            "d": model.get("d"),
            "t": model.get("t"),
            "sigma": model.get("sigma"),
            "z": model.get("z"),
            "probability_yes": model.get("probability_yes"),
            "probability_no": model.get("probability_no"),
            "model_fair_yes": model.get("probability_yes"),
            "model_fair_no": model.get("probability_no"),
            "market_yes_mid": state.orderbook.yes_mid,
            "market_no_mid": state.orderbook.no_mid,
            "market_yes_ask": state.orderbook.yes_ask,
            "market_no_ask": state.orderbook.no_ask,
            "yes_model_edge_vs_mid": _edge(_num(model.get("probability_yes")), state.orderbook.yes_mid),
            "no_model_edge_vs_mid": _edge(_num(model.get("probability_no")), state.orderbook.no_mid),
            "edge_yes": _edge(_num(model.get("probability_yes")), state.orderbook.yes_ask),
            "edge_no": _edge(_num(model.get("probability_no")), state.orderbook.no_ask),
            "yes_model_edge_vs_ask": _edge(_num(model.get("probability_yes")), state.orderbook.yes_ask),
            "no_model_edge_vs_ask": _edge(_num(model.get("probability_no")), state.orderbook.no_ask),
            "strong_side": model.get("strong_side"),
            "weak_side": model.get("weak_side"),
            "regime": model.get("regime"),
            "seconds_to_close": state.seconds_to_close,
        }

    def _record_decision(self, state: MarketState, signal: Signal, *, intent: QuoteIntent | None, model: dict[str, Any]) -> None:
        self.decision_history.append(
            {
                "ts": state.tick.ts.isoformat(),
                "market_ticker": state.contract.ticker,
                "side": signal.side,
                "reason": signal.reason,
                "target_notional": signal.target_notional,
                "estimated_shares": signal.estimated_shares,
                "limit_price": None if intent is None else intent.limit_price,
                "allowed": bool(intent.allowed) if intent is not None else False,
                "blocked_reason": None if intent is None else intent.blocked_reason,
                "projected_pair_cost": None if intent is None else intent.projected_pair_cost,
                "strong_side": model.get("strong_side"),
                "weak_side": model.get("weak_side"),
                "z": model.get("z"),
                "probability_yes": model.get("probability_yes"),
                "probability_no": model.get("probability_no"),
                **self._inventory.to_snapshot(),
            }
        )

    def _record_snapshot(self, state: MarketState, *, model: dict[str, Any]) -> None:
        self.position_history.append(
            InventoryAwareSnapshot(
                ts=state.tick.ts.isoformat(),
                market_ticker=state.contract.ticker,
                yes_qty=self._inventory.yes.qty,
                no_qty=self._inventory.no.qty,
                yes_cost=self._inventory.yes.cost,
                no_cost=self._inventory.no.cost,
                total_notional=self._inventory.total_notional,
                matched_qty=self._inventory.matched_qty,
                matched_pair_cost=self._inventory.matched_pair_cost,
                locked_edge_if_held=self._inventory.locked_edge_if_held,
                unpaired_side=self._inventory.unpaired_side,
                unpaired_qty=self._inventory.unpaired_qty,
                unpaired_notional=self._inventory.unpaired_notional(),
                strong_side=model.get("strong_side"),
                weak_side=model.get("weak_side"),
                z_score=model.get("z"),
                probability_yes=model.get("probability_yes"),
                probability_no=model.get("probability_no"),
                seconds_to_close=state.seconds_to_close,
            )
        )

    @staticmethod
    def _round_price(price: float) -> float:
        return round(max(0.0, min(0.99, price)), 4)


def _opposite(side: Side | None) -> Side | None:
    if side == "yes":
        return "no"
    if side == "no":
        return "yes"
    return None


def _signal_side_to_side(side: str) -> Side | None:
    if side == "long_above":
        return "yes"
    if side == "long_below":
        return "no"
    return None


def _edge(probability: float | str | SupportsFloat | None, market_price: float | str | SupportsFloat | None) -> float | None:
    if probability is None or market_price is None:
        return None
    try:
        return float(probability) - float(market_price)
    except (TypeError, ValueError):
        return None


def _num(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _ask_levels(book: Any) -> list[tuple[float, float]]:
    if isinstance(book, str):
        try:
            book = json.loads(book)
        except json.JSONDecodeError:
            return []
    if not isinstance(book, dict):
        return []
    raw_asks = book.get("asks")
    levels: list[tuple[float, float]] = []
    for item in raw_asks or []:
        try:
            if isinstance(item, dict):
                levels.append((float(item["price"]), float(item["size"])))
            else:
                levels.append((float(item[0]), float(item[1])))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return levels
