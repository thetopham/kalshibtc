from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..execution.paper import PaperFill
from ..market.state import MarketState
from .signals import Signal


@dataclass(frozen=True)
class SeedCheapAccumulateRepairV2Config:
    name: str = "seed_cheap_accumulate_repair_v2"
    seed_mode: str = "directional"  # directional | balanced | cheap_only
    seed_primary_spend: float = 30.0
    seed_hedge_spend: float = 10.0
    seed_min_seconds_to_close: float = 720.0
    seed_max_seconds_to_close: float = 900.0
    cheap_price: float = 0.15
    very_cheap_price: float = 0.08
    normal_spend: float = 1.0
    very_cheap_spend: float = 2.0
    max_total_cost: float = 200.0
    max_order_notional: float | None = None
    liquidity_participation_rate: float = 1.0
    min_depth_contracts: float = 0.0
    inventory_cap_contracts: float | None = None
    repair_size_multiplier: float = 1.0
    spread_penalty_enabled: bool = False
    wide_spread_threshold: float = 0.05
    spread_penalty_multiplier: float = 0.5
    cheap_persistence_seconds_for_boost: float = 60.0
    cheap_persistence_size_multiplier: float = 1.25
    min_order_contracts: float = 5.0
    one_fill_per_price_level: bool = True
    repair_max_price: float = 0.85
    emergency_repair_max_price: float | None = None
    target_net_ratio: float = 0.05
    target_pair_cost: float = 0.95
    final_neutralization_seconds: float = 120.0
    no_trade_seconds: float = 15.0
    min_seconds_between_orders: float = 0.0
    min_confidence: float = 0.70


@dataclass
class _Lot:
    qty: float = 0.0
    cost: float = 0.0

    @property
    def avg_price(self) -> float | None:
        return self.cost / self.qty if self.qty > 0 else None

    def add(self, *, qty: float, price: float) -> None:
        self.qty += qty
        self.cost += qty * price


@dataclass(frozen=True)
class SeedCheapAccumulateV2Snapshot:
    ts: str
    market_ticker: str
    yes_qty: float
    no_qty: float
    yes_cost: float
    no_cost: float
    total_cost: float
    net_qty: float
    net_ratio: float
    yes_avg_price: float | None
    no_avg_price: float | None
    avg_pair_cost: float | None
    yes_ask: float | None
    no_ask: float | None
    seconds_to_close: float
    seed_primary_side: str | None
    seed_primary_filled: bool
    seed_hedge_filled: bool


@dataclass
class SeedCheapAccumulateRepairV2Strategy:
    """Seed/cheap-accumulate strategy with inventory leakage controls only.

    V2 intentionally avoids ML/new indicators. It only tightens exposure:
    - no dominant-side unpaired adds
    - continuous opposite-side repair
    - dynamic net caps into expiry
    - final-120s neutralization only
    - pair-cost gate on pair-building inventory
    """

    config: SeedCheapAccumulateRepairV2Config = field(default_factory=SeedCheapAccumulateRepairV2Config)

    def __post_init__(self) -> None:
        if self.config.seed_mode not in {"directional", "balanced", "cheap_only"}:
            raise ValueError("seed_mode must be directional, balanced, or cheap_only")
        self._active_market_ticker: str | None = None
        self._yes = _Lot()
        self._no = _Lot()
        self._last_order_ts = None
        self._seed_primary_side: str | None = None
        self._seed_primary_filled = False
        self._seed_hedge_filled = False
        self._active_order_keys: set[str] = set()
        self._filled_order_keys: set[str] = set()
        self._cheap_seen: dict[str, Any] = {}
        self.position_history: list[SeedCheapAccumulateV2Snapshot] = []
        self.decision_history: list[dict[str, float | str | bool | None]] = []

    @property
    def name(self) -> str:
        return self.config.name

    def on_tick(self, state: MarketState) -> Signal:
        if self._active_market_ticker != state.contract.ticker:
            self._reset(state.contract.ticker)
        signal = self._choose_signal(state)
        self._record_snapshot(state)
        self._record_decision(state, signal)
        return signal

    def on_fill(self, state: MarketState, fill: PaperFill) -> None:
        side = "yes" if fill.side == "long_above" else "no" if fill.side == "long_below" else None
        if side is None:
            return
        if side == "yes":
            self._yes.add(qty=fill.contracts, price=fill.entry_price)
        else:
            self._no.add(qty=fill.contracts, price=fill.entry_price)
        if self._seed_primary_side is not None and not self._seed_primary_filled and side == self._seed_primary_side:
            self._seed_primary_filled = True
        elif self._seed_primary_side is not None and self._seed_primary_filled and not self._seed_hedge_filled and side == self._opposite(self._seed_primary_side):
            self._seed_hedge_filled = True
        self._mark_resting_order_filled(side=side, price=fill.entry_price)
        self._last_order_ts = state.tick.ts
        self._record_snapshot(state)

    def _reset(self, market_ticker: str) -> None:
        self._active_market_ticker = market_ticker
        self._yes = _Lot()
        self._no = _Lot()
        self._last_order_ts = None
        self._seed_primary_side = None
        self._seed_primary_filled = False
        self._seed_hedge_filled = False
        self._active_order_keys = set()
        self._filled_order_keys = set()
        self._cheap_seen = {}

    def _choose_signal(self, state: MarketState) -> Signal:
        yes_ask = state.orderbook.yes_ask
        no_ask = state.orderbook.no_ask
        if yes_ask is None or no_ask is None:
            return self._none("missing top-of-book asks")
        if state.seconds_to_close <= self.config.no_trade_seconds:
            if self.gross_qty <= 0 and self.config.seed_mode == "cheap_only":
                return self._none("final neutralization blocks directional adds")
            return self._none("inside no-trade settlement window")
        if self._last_order_ts is not None:
            elapsed = (state.tick.ts - self._last_order_ts).total_seconds()
            if elapsed < self.config.min_seconds_between_orders:
                return self._none("order cooldown")

        if state.seconds_to_close <= self.config.final_neutralization_seconds:
            if self.gross_qty <= 0:
                return self._none("final neutralization blocks directional adds")
            return self._repair_signal(
                yes_ask=yes_ask,
                no_ask=no_ask,
                seconds_to_close=state.seconds_to_close,
                reason="final neutralization repair",
                use_emergency=True,
                require_cap_breach=False,
                state=state,
            )

        seed_signal = self._seed_signal(state=state, yes_ask=yes_ask, no_ask=no_ask)
        if seed_signal.side != "none":
            return seed_signal
        if (
            self.config.seed_mode == "cheap_only"
            and not self._seed_primary_filled
            and seed_signal.reason == "cheap-only seed waiting for cheap side"
        ):
            return seed_signal

        repair_signal = self._repair_signal(
            yes_ask=yes_ask,
            no_ask=no_ask,
            seconds_to_close=state.seconds_to_close,
            reason="continuous repair reduces exposure",
            use_emergency=False,
            require_cap_breach=True,
            state=state,
        )
        if repair_signal.side != "none":
            return repair_signal
        if repair_signal.reason in {"dynamic net cap satisfied", "pair cost target not met", "max total cost reached"}:
            return repair_signal

        remaining = self._remaining_budget()
        if remaining <= 0:
            return self._none("max total cost reached")

        cheap_side = "yes" if yes_ask <= no_ask else "no"
        cheap_price = yes_ask if cheap_side == "yes" else no_ask
        if self._is_dominant_side(cheap_side):
            return self._none("dominant side add blocked by exposure rule")
        if cheap_price <= self.config.very_cheap_price:
            return self._buy_signal(
                side=cheap_side,
                price=cheap_price,
                spend=min(self.config.very_cheap_spend, remaining),
                reason="very cheap exposure-reducing accumulation",
                seconds_to_close=state.seconds_to_close,
                state=state,
                repair_reduces_exposure=False,
            )
        if cheap_price <= self.config.cheap_price:
            return self._buy_signal(
                side=cheap_side,
                price=cheap_price,
                spend=min(self.config.normal_spend, remaining),
                reason="cheap exposure-reducing accumulation",
                seconds_to_close=state.seconds_to_close,
                state=state,
                repair_reduces_exposure=False,
            )
        return self._none("no cheap exposure-reducing side")

    def _seed_signal(self, *, state: MarketState, yes_ask: float, no_ask: float) -> Signal:
        if not (self.config.seed_min_seconds_to_close <= state.seconds_to_close <= self.config.seed_max_seconds_to_close):
            return self._none("outside opening seed window")
        remaining = self._remaining_budget()
        if remaining <= 0:
            return self._none("max total cost reached")
        if self.config.seed_mode == "cheap_only":
            if self._seed_primary_filled:
                return self._none("cheap-only seed complete")
            cheap_side = "yes" if yes_ask <= no_ask else "no"
            cheap_price = yes_ask if cheap_side == "yes" else no_ask
            if cheap_price > self.config.cheap_price:
                return self._none("cheap-only seed waiting for cheap side")
            self._seed_primary_side = cheap_side
            return self._buy_signal(
                side=cheap_side,
                price=cheap_price,
                spend=min(self.config.normal_spend, remaining),
                reason="cheap-only seed",
                seconds_to_close=state.seconds_to_close,
                state=state,
                repair_reduces_exposure=False,
            )
        if self._seed_primary_side is None:
            self._seed_primary_side = self._primary_side(state)
        if not self._seed_primary_filled:
            side = self._seed_primary_side
            price = yes_ask if side == "yes" else no_ask
            reason = "opening directional primary" if self.config.seed_mode == "directional" else "opening balanced primary"
            return self._buy_signal(side=side, price=price, spend=min(self.config.seed_primary_spend, remaining), reason=reason, seconds_to_close=state.seconds_to_close, state=state, repair_reduces_exposure=False)
        if not self._seed_hedge_filled:
            side = self._opposite(self._seed_primary_side)
            price = yes_ask if side == "yes" else no_ask
            reason = "opening directional hedge" if self.config.seed_mode == "directional" else "opening balanced hedge"
            return self._buy_signal(side=side, price=price, spend=min(self.config.seed_hedge_spend, remaining), reason=reason, seconds_to_close=state.seconds_to_close, state=state, repair_reduces_exposure=False)
        return self._none("opening seed complete")

    def _primary_side(self, state: MarketState) -> str:
        if state.price > state.strike:
            return "yes"
        if state.price < state.strike:
            return "no"
        slope = state.slope_30s or 0.0
        return "yes" if slope >= 0 else "no"

    def _repair_signal(
        self,
        *,
        yes_ask: float,
        no_ask: float,
        seconds_to_close: float,
        reason: str,
        use_emergency: bool,
        require_cap_breach: bool,
        state: MarketState | None,
    ) -> Signal:
        if self.gross_qty <= 0:
            return self._none("repair with no inventory")
        cap = self._dynamic_net_cap(seconds_to_close)
        if require_cap_breach and self.net_ratio <= cap:
            return self._none("dynamic net cap satisfied")
        if self.net_ratio <= self.config.target_net_ratio:
            return self._none("repair target net ratio reached")
        if self._yes.qty > self._no.qty:
            side = "no"
            price = no_ask
            contracts_needed = self._yes.qty - self._no.qty
        elif self._no.qty > self._yes.qty:
            side = "yes"
            price = yes_ask
            contracts_needed = self._no.qty - self._yes.qty
        else:
            return self._none("already fully paired")
        max_price = self.config.repair_max_price
        if use_emergency and self.config.emergency_repair_max_price is not None:
            max_price = self.config.emergency_repair_max_price
        if price > max_price:
            return self._none("repair side too expensive")
        pair_cost_allowed = self._pair_cost_allowed_contracts(side=side, price=price)
        if pair_cost_allowed <= 0:
            return self._none("pair cost target not met")
        remaining = self._remaining_budget()
        if remaining <= 0:
            return self._none("max total cost reached")
        spend = min(contracts_needed * price, remaining)
        return self._buy_signal(side=side, price=price, spend=spend, reason=reason, seconds_to_close=seconds_to_close, state=state, repair_reduces_exposure=True)

    def _pair_cost_allows(self, *, side: str, price: float, qty: float) -> bool:
        yes_qty = self._yes.qty + (qty if side == "yes" else 0.0)
        no_qty = self._no.qty + (qty if side == "no" else 0.0)
        yes_cost = self._yes.cost + (qty * price if side == "yes" else 0.0)
        no_cost = self._no.cost + (qty * price if side == "no" else 0.0)
        yes_avg = yes_cost / yes_qty if yes_qty > 0 else None
        no_avg = no_cost / no_qty if no_qty > 0 else None
        if yes_avg is None or no_avg is None:
            return True
        return yes_avg + no_avg <= self.config.target_pair_cost

    def _dynamic_net_cap(self, seconds_to_close: float) -> float:
        if seconds_to_close < 120:
            return 0.05
        if seconds_to_close < 360:
            return 0.15
        if seconds_to_close < 720:
            return 0.20
        return 0.25

    @staticmethod
    def _opposite(side: str) -> str:
        return "no" if side == "yes" else "yes"

    @staticmethod
    def _order_key(*, side: str, price: float, reason: str) -> str:
        return f"{side}:{reason}:{round(price, 4):.4f}"

    def _mark_resting_order_filled(self, *, side: str, price: float) -> None:
        prefix = f"{side}:"
        price_suffix = f":{round(price, 4):.4f}"
        matched = {key for key in self._active_order_keys if key.startswith(prefix) and key.endswith(price_suffix)}
        self._active_order_keys.difference_update(matched)
        self._filled_order_keys.update(matched)

    def _buy_signal(
        self,
        *,
        side: str,
        price: float,
        spend: float,
        reason: str,
        seconds_to_close: float,
        state: MarketState | None = None,
        repair_reduces_exposure: bool = False,
    ) -> Signal:
        if price <= 0 or spend <= 0:
            return self._none("non-positive spend or price")
        sized = self._liquidity_sized_order(
            side=side,
            price=price,
            requested_spend=spend,
            state=state,
            repair_reduces_exposure=repair_reduces_exposure,
        )
        if sized["reason"] is not None:
            return self._none(str(sized["reason"]))
        contracts = float(sized["contracts"])
        spend = contracts * price
        if contracts < self.config.min_order_contracts:
            return self._none("below min order contracts")
        order_key = self._order_key(side=side, price=price, reason=reason)
        if self.config.one_fill_per_price_level:
            if order_key in self._active_order_keys:
                return self._none("resting limit order already active")
            if order_key in self._filled_order_keys:
                return self._none("resting limit price level already filled")
            self._active_order_keys.add(order_key)
        return Signal(
            "long_above" if side == "yes" else "long_below",
            reason,
            self.config.min_confidence,
            strategy=self.name,
            target_notional=round(spend, 6),
            estimated_shares=contracts,
            features={
                "limit_price": price,
                "price": price,
                "spend": spend,
                **sized["features"],
                "remaining_budget": self._remaining_budget(),
                "total_cost": self.total_cost,
                "net_qty": self.net_qty,
                "net_ratio": self.net_ratio,
                "dynamic_net_cap": self._dynamic_net_cap(seconds_to_close),
                "target_pair_cost": self.config.target_pair_cost,
                "avg_pair_cost": self.avg_pair_cost,
                "min_order_contracts": self.config.min_order_contracts,
                "resting_order_key": order_key,
                "seed_mode": self.config.seed_mode,
                "seed_primary_side": self._seed_primary_side,
                "seed_primary_filled": self._seed_primary_filled,
                "seed_hedge_filled": self._seed_hedge_filled,
                "seconds_to_close": seconds_to_close,
            },
            allow_price_strike_mismatch=True,
        )

    def _liquidity_sized_order(
        self,
        *,
        side: str,
        price: float,
        requested_spend: float,
        state: MarketState | None,
        repair_reduces_exposure: bool,
    ) -> dict[str, Any]:
        base_contracts = requested_spend / price
        if self.config.max_order_notional is not None:
            base_contracts = min(base_contracts, self.config.max_order_notional / price)
        if repair_reduces_exposure:
            base_contracts *= self.config.repair_size_multiplier
        visible_depth = self._visible_ask_depth(side=side, limit_price=price, state=state)
        if visible_depth is not None and visible_depth < self.config.min_depth_contracts:
            return {"reason": "insufficient visible ask depth", "contracts": 0.0, "features": {"visible_depth_contracts": visible_depth}}
        depth_cap = visible_depth * self.config.liquidity_participation_rate if visible_depth is not None else base_contracts
        remaining_market_exposure_contracts = self._remaining_budget() / price
        inventory_cap_remaining_contracts = self._inventory_cap_remaining(side)
        pair_cost_allowed_contracts = self._pair_cost_allowed_contracts(side=side, price=price)
        contracts = min(base_contracts, depth_cap, remaining_market_exposure_contracts, inventory_cap_remaining_contracts, pair_cost_allowed_contracts)
        spread = self._side_spread(side=side, state=state)
        spread_multiplier = 1.0
        if self.config.spread_penalty_enabled and spread is not None and spread > self.config.wide_spread_threshold:
            spread_multiplier = self.config.spread_penalty_multiplier
            contracts *= spread_multiplier
            spread_penalized = True
        else:
            spread_penalized = False
        persistence_multiplier = self._cheap_persistence_multiplier(side=side, price=price, state=state)
        if persistence_multiplier > 1.0 and not spread_penalized:
            hard_cap = min(depth_cap, remaining_market_exposure_contracts, inventory_cap_remaining_contracts, pair_cost_allowed_contracts)
            contracts = min(contracts * persistence_multiplier, hard_cap)
        return {
            "reason": None,
            "contracts": max(0.0, contracts),
            "features": {
                "base_contracts": base_contracts,
                "visible_depth_contracts": visible_depth,
                "liquidity_participation_rate": self.config.liquidity_participation_rate,
                "remaining_market_exposure_contracts": remaining_market_exposure_contracts,
                "inventory_cap_remaining_contracts": inventory_cap_remaining_contracts,
                "pair_cost_allowed_contracts": pair_cost_allowed_contracts,
                "repair_reduces_exposure": repair_reduces_exposure,
                "spread": spread,
                "spread_penalty_multiplier": spread_multiplier,
                "cheap_persistence_multiplier": persistence_multiplier,
            },
        }

    def _visible_ask_depth(self, *, side: str, limit_price: float, state: MarketState | None) -> float | None:
        if state is None:
            return None
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
        levels = self._ask_levels(book)
        if not levels:
            return None
        return sum(size for level_price, size in levels if level_price <= limit_price + 1e-9)

    @staticmethod
    def _ask_levels(book: Any) -> list[tuple[float, float]]:
        if isinstance(book, str):
            try:
                book = json.loads(book)
            except json.JSONDecodeError:
                return []
        if not isinstance(book, dict):
            return []
        raw_asks = book.get("asks")
        levels = []
        for item in raw_asks or []:
            try:
                if isinstance(item, dict):
                    levels.append((float(item["price"]), float(item["size"])))
                else:
                    levels.append((float(item[0]), float(item[1])))
            except (KeyError, IndexError, TypeError, ValueError):
                continue
        return levels

    def _side_spread(self, *, side: str, state: MarketState | None) -> float | None:
        if state is None:
            return None
        return state.orderbook.yes_spread if side == "yes" else state.orderbook.no_spread

    def _inventory_cap_remaining(self, side: str) -> float:
        if self.config.inventory_cap_contracts is None:
            return float("inf")
        current = self._yes.qty if side == "yes" else self._no.qty
        return max(0.0, self.config.inventory_cap_contracts - current)

    def _pair_cost_allowed_contracts(self, *, side: str, price: float) -> float:
        if side == "yes":
            other_qty = self._no.qty
            same_qty = self._yes.qty
            same_cost = self._yes.cost
            other_avg = self._no.cost / self._no.qty if self._no.qty > 0 else None
        else:
            other_qty = self._yes.qty
            same_qty = self._no.qty
            same_cost = self._no.cost
            other_avg = self._yes.cost / self._yes.qty if self._yes.qty > 0 else None
        _ = other_qty
        if other_avg is None:
            return float("inf")
        max_avg = self.config.target_pair_cost - other_avg
        if max_avg < price:
            numerator = max_avg * same_qty - same_cost
            denominator = price - max_avg
            if denominator <= 0:
                return float("inf")
            return max(0.0, numerator / denominator)
        return float("inf")

    def _cheap_persistence_multiplier(self, *, side: str, price: float, state: MarketState | None) -> float:
        if state is None or price > self.config.cheap_price:
            self._cheap_seen.pop(side, None)
            return 1.0
        first = self._cheap_seen.setdefault(side, state.tick.ts)
        persisted = (state.tick.ts - first).total_seconds()
        if persisted >= self.config.cheap_persistence_seconds_for_boost:
            return self.config.cheap_persistence_size_multiplier
        return 1.0

    def _none(self, reason: str) -> Signal:
        return Signal("none", reason, 0.0, strategy=self.name, allow_price_strike_mismatch=True)

    def _remaining_budget(self) -> float:
        return max(0.0, self.config.max_total_cost - self.total_cost)

    @property
    def total_cost(self) -> float:
        return self._yes.cost + self._no.cost

    @property
    def gross_qty(self) -> float:
        return self._yes.qty + self._no.qty

    @property
    def net_qty(self) -> float:
        return self._yes.qty - self._no.qty

    @property
    def net_ratio(self) -> float:
        denom = max(self._yes.qty, self._no.qty)
        return abs(self.net_qty) / denom if denom > 0 else 0.0

    @property
    def avg_pair_cost(self) -> float | None:
        if self._yes.qty <= 0 or self._no.qty <= 0:
            return None
        return (self._yes.cost / self._yes.qty) + (self._no.cost / self._no.qty)

    def _is_dominant_side(self, side: str) -> bool:
        if side == "yes":
            return self._yes.qty > self._no.qty
        return self._no.qty > self._yes.qty

    def _record_decision(self, state: MarketState, signal: Signal) -> None:
        self.decision_history.append(
            {
                "ts": state.tick.ts.isoformat(),
                "market_ticker": state.contract.ticker,
                "side": signal.side,
                "reason": signal.reason,
                "target_notional": signal.target_notional,
                "estimated_shares": signal.estimated_shares,
                "total_cost": self.total_cost,
                "net_qty": self.net_qty,
                "net_ratio": self.net_ratio,
                "dynamic_net_cap": self._dynamic_net_cap(state.seconds_to_close),
                "avg_pair_cost": self.avg_pair_cost,
                "seed_primary_side": self._seed_primary_side,
                "seed_primary_filled": self._seed_primary_filled,
                "seed_hedge_filled": self._seed_hedge_filled,
            }
        )

    def _record_snapshot(self, state: MarketState) -> None:
        self.position_history.append(
            SeedCheapAccumulateV2Snapshot(
                ts=state.tick.ts.isoformat(),
                market_ticker=state.contract.ticker,
                yes_qty=self._yes.qty,
                no_qty=self._no.qty,
                yes_cost=self._yes.cost,
                no_cost=self._no.cost,
                total_cost=self.total_cost,
                net_qty=self.net_qty,
                net_ratio=self.net_ratio,
                yes_avg_price=self._yes.avg_price,
                no_avg_price=self._no.avg_price,
                avg_pair_cost=self.avg_pair_cost,
                yes_ask=state.orderbook.yes_ask,
                no_ask=state.orderbook.no_ask,
                seconds_to_close=state.seconds_to_close,
                seed_primary_side=self._seed_primary_side,
                seed_primary_filled=self._seed_primary_filled,
                seed_hedge_filled=self._seed_hedge_filled,
            )
        )
