from __future__ import annotations

from dataclasses import dataclass, field

from ..execution.paper import PaperFill
from ..market.state import MarketState
from .signals import Signal


@dataclass(frozen=True)
class SeedCheapAccumulateRepairConfig:
    name: str = "seed_cheap_accumulate_repair_v1"
    seed_primary_spend: float = 30.0
    seed_hedge_spend: float = 10.0
    seed_min_seconds_to_close: float = 720.0
    seed_max_seconds_to_close: float = 900.0
    cheap_price: float = 0.15
    very_cheap_price: float = 0.08
    normal_spend: float = 1.0
    very_cheap_spend: float = 2.0
    max_total_cost: float = 200.0
    max_net_ratio: float = 0.35
    min_order_contracts: float = 5.0
    one_fill_per_price_level: bool = True
    repair_start_seconds: float = 240.0
    repair_max_price: float = 0.85
    target_net_ratio: float = 0.05
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
class SeedCheapAccumulateSnapshot:
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
    yes_ask: float | None
    no_ask: float | None
    seconds_to_close: float
    seed_primary_side: str | None
    seed_primary_filled: bool
    seed_hedge_filled: bool


@dataclass
class SeedCheapAccumulateRepairV1Strategy:
    """3:1 opening seed, then tiny cheap adds, then repair.

    This is intentionally simpler than v0:
    - no direct-pair threshold/path
    - one opening seed per market while the contract is near open
    - primary seed side follows BTC vs strike, hedge is the opposite side
    - after seed, only buy sides that are objectively cheap
    - repair window only buys the smaller contract side
    """

    config: SeedCheapAccumulateRepairConfig = field(default_factory=SeedCheapAccumulateRepairConfig)

    def __post_init__(self) -> None:
        self._active_market_ticker: str | None = None
        self._yes = _Lot()
        self._no = _Lot()
        self._last_order_ts = None
        self._seed_primary_side: str | None = None
        self._seed_primary_filled = False
        self._seed_hedge_filled = False
        self._active_order_keys: set[str] = set()
        self._filled_order_keys: set[str] = set()
        self.position_history: list[SeedCheapAccumulateSnapshot] = []
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
        phase = (fill.strategy, getattr(fill, "reason", None))
        signal_reason = None
        # PaperFill does not carry the signal reason, so infer seed completion from side/order.
        if self._seed_primary_side is not None and not self._seed_primary_filled and side == self._seed_primary_side:
            self._seed_primary_filled = True
            signal_reason = "opening seed primary"
        elif self._seed_primary_side is not None and self._seed_primary_filled and not self._seed_hedge_filled and side == self._opposite(self._seed_primary_side):
            self._seed_hedge_filled = True
            signal_reason = "opening seed hedge"
        self._mark_resting_order_filled(side=side, price=fill.entry_price)
        _ = phase, signal_reason
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

    def _choose_signal(self, state: MarketState) -> Signal:
        yes_ask = state.orderbook.yes_ask
        no_ask = state.orderbook.no_ask
        if yes_ask is None or no_ask is None:
            return self._none("missing top-of-book asks")
        if state.seconds_to_close <= self.config.no_trade_seconds:
            return self._none("inside no-trade settlement window")
        if self._last_order_ts is not None:
            elapsed = (state.tick.ts - self._last_order_ts).total_seconds()
            if elapsed < self.config.min_seconds_between_orders:
                return self._none("order cooldown")

        if state.seconds_to_close <= self.config.repair_start_seconds:
            return self._repair_signal(yes_ask=yes_ask, no_ask=no_ask, seconds_to_close=state.seconds_to_close)

        seed_signal = self._seed_signal(state=state, yes_ask=yes_ask, no_ask=no_ask)
        if seed_signal.side != "none":
            return seed_signal

        remaining = self._remaining_budget()
        if remaining <= 0:
            return self._none("max total cost reached")

        cheap_side = "yes" if yes_ask <= no_ask else "no"
        cheap_price = yes_ask if cheap_side == "yes" else no_ask
        if self._is_dominant_side(cheap_side) and self.gross_net_ratio > self.config.max_net_ratio:
            return self._none("dominant side blocked by max net ratio")
        if cheap_price <= self.config.very_cheap_price:
            return self._buy_signal(
                side=cheap_side,
                price=cheap_price,
                spend=min(self.config.very_cheap_spend, remaining),
                reason="very cheap side accumulation",
                seconds_to_close=state.seconds_to_close,
            )
        if cheap_price <= self.config.cheap_price:
            return self._buy_signal(
                side=cheap_side,
                price=cheap_price,
                spend=min(self.config.normal_spend, remaining),
                reason="cheap side accumulation",
                seconds_to_close=state.seconds_to_close,
            )
        return self._none("no cheap side")

    def _seed_signal(self, *, state: MarketState, yes_ask: float, no_ask: float) -> Signal:
        if not (self.config.seed_min_seconds_to_close <= state.seconds_to_close <= self.config.seed_max_seconds_to_close):
            return self._none("outside opening seed window")
        if self._seed_primary_side is None:
            self._seed_primary_side = self._primary_side(state)
        remaining = self._remaining_budget()
        if remaining <= 0:
            return self._none("max total cost reached")
        if not self._seed_primary_filled:
            side = self._seed_primary_side
            price = yes_ask if side == "yes" else no_ask
            return self._buy_signal(
                side=side,
                price=price,
                spend=min(self.config.seed_primary_spend, remaining),
                reason="opening seed primary 3x",
                seconds_to_close=state.seconds_to_close,
            )
        if not self._seed_hedge_filled:
            side = self._opposite(self._seed_primary_side)
            price = yes_ask if side == "yes" else no_ask
            return self._buy_signal(
                side=side,
                price=price,
                spend=min(self.config.seed_hedge_spend, remaining),
                reason="opening seed hedge 1x",
                seconds_to_close=state.seconds_to_close,
            )
        return self._none("opening seed complete")

    def _primary_side(self, state: MarketState) -> str:
        if state.price > state.strike:
            return "yes"
        if state.price < state.strike:
            return "no"
        slope = state.slope_30s or 0.0
        return "yes" if slope >= 0 else "no"

    def _repair_signal(self, *, yes_ask: float, no_ask: float, seconds_to_close: float) -> Signal:
        if self.gross_qty <= 0:
            return self._none("repair window with no inventory")
        if self.net_ratio <= self.config.target_net_ratio:
            return self._none("repair target net ratio reached")
        if self._yes.qty > self._no.qty:
            side = "no"
            price = no_ask
            contracts_needed = self._yes.qty - self._no.qty
        else:
            side = "yes"
            price = yes_ask
            contracts_needed = self._no.qty - self._yes.qty
        if price > self.config.repair_max_price:
            return self._none("repair side too expensive")
        remaining = self._remaining_budget()
        if remaining <= 0:
            return self._none("max total cost reached")
        return self._buy_signal(
            side=side,
            price=price,
            spend=min(contracts_needed * price, remaining),
            reason="repair smaller side near expiry",
            seconds_to_close=seconds_to_close,
        )

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

    def _buy_signal(self, *, side: str, price: float, spend: float, reason: str, seconds_to_close: float) -> Signal:
        if price <= 0 or spend <= 0:
            return self._none("non-positive spend or price")
        contracts = spend / price
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
                "remaining_budget": self._remaining_budget(),
                "total_cost": self.total_cost,
                "net_qty": self.net_qty,
                "net_ratio": self.net_ratio,
                "gross_net_ratio": self.gross_net_ratio,
                "max_net_ratio": self.config.max_net_ratio,
                "min_order_contracts": self.config.min_order_contracts,
                "resting_order_key": order_key,
                "seed_primary_side": self._seed_primary_side,
                "seed_primary_filled": self._seed_primary_filled,
                "seed_hedge_filled": self._seed_hedge_filled,
                "seconds_to_close": seconds_to_close,
            },
            allow_price_strike_mismatch=True,
        )

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
    def gross_net_ratio(self) -> float:
        return abs(self.net_qty) / self.gross_qty if self.gross_qty > 0 else 0.0

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
                "gross_net_ratio": self.gross_net_ratio,
                "seed_primary_side": self._seed_primary_side,
                "seed_primary_filled": self._seed_primary_filled,
                "seed_hedge_filled": self._seed_hedge_filled,
            }
        )

    def _record_snapshot(self, state: MarketState) -> None:
        self.position_history.append(
            SeedCheapAccumulateSnapshot(
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
                yes_ask=state.orderbook.yes_ask,
                no_ask=state.orderbook.no_ask,
                seconds_to_close=state.seconds_to_close,
                seed_primary_side=self._seed_primary_side,
                seed_primary_filled=self._seed_primary_filled,
                seed_hedge_filled=self._seed_hedge_filled,
            )
        )
