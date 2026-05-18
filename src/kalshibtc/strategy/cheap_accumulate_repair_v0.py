from __future__ import annotations

from dataclasses import dataclass, field

from ..execution.paper import PaperFill
from ..market.pricing import entry_price_for_signal
from ..market.state import MarketState
from .signals import Signal


@dataclass(frozen=True)
class CheapAccumulateRepairConfig:
    name: str = "cheap_accumulate_repair_v0"
    cheap_price: float = 0.25
    very_cheap_price: float = 0.10
    normal_spend: float = 10.0
    very_cheap_spend: float = 25.0
    direct_pair_cost: float = 0.95
    direct_pair_spend: float = 20.0
    max_total_cost: float = 200.0
    repair_start_seconds: float = 120.0
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
class CheapAccumulateSnapshot:
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


@dataclass
class CheapAccumulateRepairV0Strategy:
    """Buy cheap optionality, then repair/balance near expiry.

    This intentionally avoids shock detection, trend labels, distance filters, and
    lot-level pair bookkeeping. The book prices already encode time/distance.
    Before the repair window it buys whichever side is cheap under a per-market
    dollar cap. Inside the repair window it stops opening new exposure and only
    buys the smaller side to reduce quantity imbalance.
    """

    config: CheapAccumulateRepairConfig = field(default_factory=CheapAccumulateRepairConfig)

    def __post_init__(self) -> None:
        self._active_market_ticker: str | None = None
        self._yes = _Lot()
        self._no = _Lot()
        self._last_order_ts = None
        self.position_history: list[CheapAccumulateSnapshot] = []
        self.decision_history: list[dict[str, float | str | None]] = []

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
        if fill.side == "long_above":
            self._yes.add(qty=fill.contracts, price=fill.entry_price)
        elif fill.side == "long_below":
            self._no.add(qty=fill.contracts, price=fill.entry_price)
        else:
            return
        self._last_order_ts = state.tick.ts
        self._record_snapshot(state)

    def _reset(self, market_ticker: str) -> None:
        self._active_market_ticker = market_ticker
        self._yes = _Lot()
        self._no = _Lot()
        self._last_order_ts = None

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

        remaining = self._remaining_budget()
        if remaining <= 0:
            return self._none("max total cost reached")

        if yes_ask + no_ask <= self.config.direct_pair_cost:
            side = self._smaller_side_or_cheaper(yes_ask=yes_ask, no_ask=no_ask)
            price = yes_ask if side == "yes" else no_ask
            spend = min(self.config.direct_pair_spend, remaining)
            return self._buy_signal(
                side=side,
                price=price,
                spend=spend,
                reason="direct pair cheap; add smaller/cheaper side",
                seconds_to_close=state.seconds_to_close,
            )

        cheap_side = "yes" if yes_ask <= no_ask else "no"
        cheap_price = yes_ask if cheap_side == "yes" else no_ask
        if cheap_price <= self.config.very_cheap_price:
            spend = min(self.config.very_cheap_spend, remaining)
            return self._buy_signal(
                side=cheap_side,
                price=cheap_price,
                spend=spend,
                reason="very cheap side accumulation",
                seconds_to_close=state.seconds_to_close,
            )
        if cheap_price <= self.config.cheap_price:
            spend = min(self.config.normal_spend, remaining)
            return self._buy_signal(
                side=cheap_side,
                price=cheap_price,
                spend=spend,
                reason="cheap side accumulation",
                seconds_to_close=state.seconds_to_close,
            )
        return self._none("no cheap side")

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
        spend_needed = contracts_needed * price
        spend = min(spend_needed, remaining)
        return self._buy_signal(
            side=side,
            price=price,
            spend=spend,
            reason="repair smaller side near expiry",
            seconds_to_close=seconds_to_close,
        )

    def _smaller_side_or_cheaper(self, *, yes_ask: float, no_ask: float) -> str:
        if self._yes.qty < self._no.qty:
            return "yes"
        if self._no.qty < self._yes.qty:
            return "no"
        return "yes" if yes_ask <= no_ask else "no"

    def _buy_signal(self, *, side: str, price: float, spend: float, reason: str, seconds_to_close: float) -> Signal:
        if price <= 0 or spend <= 0:
            return self._none("non-positive spend or price")
        paper_side = "long_above" if side == "yes" else "long_below"
        return Signal(
            paper_side,
            reason,
            self.config.min_confidence,
            strategy=self.name,
            target_notional=round(spend, 6),
            estimated_shares=spend / price,
            features={
                "limit_price": price,
                "price": price,
                "spend": spend,
                "remaining_budget": self._remaining_budget(),
                "total_cost": self.total_cost,
                "net_qty": self.net_qty,
                "net_ratio": self.net_ratio,
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
            }
        )

    def _record_snapshot(self, state: MarketState) -> None:
        self.position_history.append(
            CheapAccumulateSnapshot(
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
            )
        )
