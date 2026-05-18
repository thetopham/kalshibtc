from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from ..execution.paper import PaperFill
from ..market.pricing import entry_price_for_signal
from ..market.state import MarketState
from .signals import Signal


@dataclass(frozen=True)
class SimpleInventoryMMConfig:
    """Small passive inventory/MM strategy knobs.

    This is intentionally simpler than hedge_volatility_v0.  It treats the
    orderbook as a passive-touch simulator: a limit order is considered filled
    only when the currently recorded ask is at or below the strategy's limit.
    The replay executor still records an ask-side fill, so this is conservative
    versus a real maker fill, but it avoids buying through arbitrary asks.
    """

    name: str = "simple_inventory_mm"
    seed_contracts_per_side: float = 50.0
    seed_add_contracts: float = 10.0
    seed_limit_price: float = 0.58
    cheap_limit_price: float = 0.35
    very_cheap_price: float = 0.20
    cheap_add_contracts: float = 12.0
    very_cheap_add_contracts: float = 20.0
    rich_add_contracts: float = 4.0
    rich_limit_price: float = 0.82
    repair_window_seconds: float = 180.0
    repair_max_price: float = 0.90
    repair_add_contracts: float = 12.0
    max_net_contracts: float = 100.0
    max_gross_contracts: float = 180.0
    min_seconds_between_orders: float = 5.0
    min_seconds_to_close: float = 45.0
    max_seed_seconds_to_close: float = 840.0
    min_improvement_cents: float = 0.01
    min_confidence: float = 0.70


@dataclass
class _Lot:
    qty: float = 0.0
    avg_price: float = 0.0

    @property
    def cost(self) -> float:
        return self.qty * self.avg_price

    def add(self, qty: float, price: float) -> None:
        total_cost = self.cost + qty * price
        self.qty += qty
        self.avg_price = total_cost / self.qty if self.qty > 0 else 0.0


@dataclass
class _RestingOrder:
    side: str
    reason: str
    limit_price: float
    contracts: float
    level_key: str
    placed_ts: datetime
    filled: bool = False


@dataclass(frozen=True)
class SimpleInventorySnapshot:
    ts: str
    market_ticker: str
    yes_qty: float
    yes_avg_price: float
    no_qty: float
    no_avg_price: float
    raw_net_contracts: float
    effective_mtm_exposure: float
    gross_contracts: float
    combined_basis: float | None
    yes_ask: float | None
    no_ask: float | None


@dataclass
class SimpleInventoryMMStrategy:
    """Passive bounded-inventory strategy for Kalshi BTC ABOVE/BELOW contracts.

    Thesis:
    - Start roughly balanced when both sides are not too expensive.
    - When BTC moves and one side collapses, average down that cheap side.
    - That cheap inventory lowers basis / directional risk and frees limited
      capacity to lightly add the rich side again.
    - Never cross arbitrary asks; only emit a buy signal when ask <= the limit
      this strategy would have posted.
    """

    config: SimpleInventoryMMConfig = field(default_factory=SimpleInventoryMMConfig)

    def __post_init__(self) -> None:
        self._active_market_ticker: str | None = None
        self._yes = _Lot()
        self._no = _Lot()
        self.position_history: list[SimpleInventorySnapshot] = []
        self.decision_history: list[dict[str, float | str | None]] = []
        self._last_order_ts = None
        self._resting_orders: dict[str, _RestingOrder] = {}
        self._filled_order_keys: set[str] = set()

    @property
    def name(self) -> str:
        return self.config.name

    def on_tick(self, state: MarketState) -> Signal:
        if self._active_market_ticker != state.contract.ticker:
            self._reset(state.contract.ticker)

        side, reason, limit_price, contracts = self._choose_order(state)
        self._record_snapshot(state)
        if side == "none" or limit_price is None or contracts <= 0:
            signal = Signal("none", reason, 0.0, strategy=self.name, allow_price_strike_mismatch=True)
            self._record_decision(state, signal, limit_price=limit_price, contracts=contracts)
            return signal

        level_key = self._level_key(side, reason, limit_price)
        order = self._resting_orders.get(level_key)
        if order is not None:
            signal = Signal("none", "resting passive order already active", 0.0, strategy=self.name, allow_price_strike_mismatch=True)
            self._record_decision(state, signal, limit_price=limit_price, contracts=0.0)
            return signal
        if level_key in self._filled_order_keys:
            signal = Signal("none", "resting passive order already filled", 0.0, strategy=self.name, allow_price_strike_mismatch=True)
            self._record_decision(state, signal, limit_price=limit_price, contracts=0.0)
            return signal

        ask = entry_price_for_signal(side, state.orderbook)
        if ask is None:
            signal = Signal("none", "missing ask for passive order", 0.0, strategy=self.name, allow_price_strike_mismatch=True)
            self._record_decision(state, signal, limit_price=limit_price, contracts=contracts)
            return signal
        if ask > limit_price:
            signal = Signal(
                "none",
                f"passive limit not touched: ask {ask:.3f} > limit {limit_price:.3f}",
                0.0,
                strategy=self.name,
                allow_price_strike_mismatch=True,
            )
            self._record_decision(state, signal, limit_price=limit_price, contracts=contracts)
            return signal

        contracts = self._clamp_contracts_to_capacity(side, contracts)
        if contracts <= 0:
            signal = Signal("none", "projected inventory cap exhausted", 0.0, strategy=self.name, allow_price_strike_mismatch=True)
            self._record_decision(state, signal, limit_price=limit_price, contracts=0.0)
            return signal
        self._resting_orders[level_key] = _RestingOrder(
            side=side,
            reason=reason,
            limit_price=limit_price,
            contracts=contracts,
            level_key=level_key,
            placed_ts=state.tick.ts,
        )
        target_notional = round(contracts * ask, 6)
        features = self._features(state, limit_price=limit_price, contracts=contracts)
        features["resting_order_key"] = level_key
        signal = Signal(
            side=side,
            reason=reason,
            confidence=self.config.min_confidence,
            strategy=self.name,
            target_notional=target_notional,
            estimated_shares=contracts,
            features=features,
            allow_price_strike_mismatch=True,
        )
        self._record_decision(state, signal, limit_price=limit_price, contracts=contracts)
        return signal

    def on_fill(self, state: MarketState, fill: PaperFill) -> None:
        if fill.side not in {"long_above", "long_below"}:
            return
        order_key = self._order_key_for_fill(fill)
        adjusted = self._cap_fill_to_inventory_limits(fill)
        if adjusted is None:
            self._last_order_ts = state.tick.ts
            self._remove_resting_order(order_key)
            self._record_snapshot(state)
            return
        if adjusted.side == "long_above":
            self._yes.add(adjusted.contracts, adjusted.entry_price)
        elif adjusted.side == "long_below":
            self._no.add(adjusted.contracts, adjusted.entry_price)
        self._last_order_ts = state.tick.ts
        self._remove_resting_order(order_key)
        self._record_snapshot(state)

    def _reset(self, market_ticker: str) -> None:
        self._active_market_ticker = market_ticker
        self._yes = _Lot()
        self._no = _Lot()
        self._resting_orders = {}
        self._filled_order_keys = set()

    def _level_key(self, side: str, reason: str, limit_price: float) -> str:
        return f"{side}:{reason}:{limit_price:.4f}"

    def _order_key_for_fill(self, fill: PaperFill) -> str | None:
        # PaperFill does not carry the signal, so infer the active order by side.
        side_orders = [key for key, order in self._resting_orders.items() if order.side == fill.side]
        if len(side_orders) == 1:
            return side_orders[0]
        return None

    def _remove_resting_order(self, order_key: str | None) -> None:
        if order_key is not None:
            self._resting_orders.pop(order_key, None)
            self._filled_order_keys.add(order_key)

    def _choose_order(self, state: MarketState) -> tuple[str, str, float | None, float]:
        if state.seconds_to_close <= self.config.min_seconds_to_close:
            return "none", "too close to expiry for passive inventory", None, 0.0
        if self._last_order_ts is not None:
            elapsed = (state.tick.ts - self._last_order_ts).total_seconds()
            if elapsed < self.config.min_seconds_between_orders:
                return "none", "passive order cooldown", None, 0.0
        yes_ask = state.orderbook.yes_ask
        no_ask = state.orderbook.no_ask
        if yes_ask is None or no_ask is None:
            return "none", "missing top-of-book asks", None, 0.0
        if self.gross_contracts >= self.config.max_gross_contracts:
            return "none", "max gross inventory reached", None, 0.0

        if state.seconds_to_close <= self.config.repair_window_seconds:
            return self._repair_order(yes_ask=yes_ask, no_ask=no_ask)

        # Seed roughly balanced inventory early/mid contract, one passive touch at a time.
        if state.seconds_to_close <= self.config.max_seed_seconds_to_close:
            if self._yes.qty < self.config.seed_contracts_per_side and yes_ask <= self.config.seed_limit_price:
                return (
                    "long_above",
                    "seed balanced yes inventory at passive limit",
                    self.config.seed_limit_price,
                    min(self.config.seed_add_contracts, self.config.seed_contracts_per_side - self._yes.qty),
                )
            if self._no.qty < self.config.seed_contracts_per_side and no_ask <= self.config.seed_limit_price:
                return (
                    "long_below",
                    "seed balanced no inventory at passive limit",
                    self.config.seed_limit_price,
                    min(self.config.seed_add_contracts, self.config.seed_contracts_per_side - self._no.qty),
                )

        cheap_side = "long_above" if yes_ask <= no_ask else "long_below"
        cheap_ask = yes_ask if cheap_side == "long_above" else no_ask
        rich_side = "long_below" if cheap_side == "long_above" else "long_above"
        rich_ask = no_ask if rich_side == "long_below" else yes_ask

        if cheap_ask <= self.config.cheap_limit_price:
            add_contracts = self.config.very_cheap_add_contracts if cheap_ask <= self.config.very_cheap_price else self.config.cheap_add_contracts
            if self._improves_avg(cheap_side, cheap_ask) and self._clamp_contracts_to_capacity(cheap_side, add_contracts) > 0 and self._net_is_within_caps():
                return cheap_side, "average down collapsed side at passive limit", self.config.cheap_limit_price, add_contracts

        # After cheap-side averaging, allow a small add to the rich/favored side
        # if raw directional capacity still has room. This matches the example:
        # 56 YES, 33 NO => net 23, leeway to max_net 100 is 77.
        if rich_ask <= self.config.rich_limit_price:
            contracts = self.config.rich_add_contracts
            if self._has_basis_credit(opposite_side=cheap_side) and self._within_caps(rich_side, contracts):
                return rich_side, "light rich-side add using freed inventory capacity", self.config.rich_limit_price, contracts

        return "none", "no passive inventory limit touched", None, 0.0

    def _repair_order(self, *, yes_ask: float, no_ask: float) -> tuple[str, str, float | None, float]:
        raw_net = self._yes.qty - self._no.qty
        if raw_net == 0:
            return "none", "repair window already flat", None, 0.0
        side = "long_below" if raw_net > 0 else "long_above"
        ask = no_ask if side == "long_below" else yes_ask
        if ask > self.config.repair_max_price:
            return "none", "repair smaller side too expensive", None, 0.0
        needed = abs(raw_net)
        contracts = min(self.config.repair_add_contracts, needed)
        if contracts <= 0:
            return "none", "repair window already flat", None, 0.0
        if not self._within_caps(side, contracts):
            return "none", "repair order blocked by gross/net caps", None, 0.0
        return side, "final-window repair smaller side only", self.config.repair_max_price, contracts

    def _improves_avg(self, side: str, price: float) -> bool:
        lot = self._yes if side == "long_above" else self._no
        if lot.qty <= 0:
            return True
        return price <= lot.avg_price - self.config.min_improvement_cents

    def _has_basis_credit(self, *, opposite_side: str) -> bool:
        # The opposite/collapsed side must exist and have been averaged below
        # its starting cost or below the cheap threshold before we allow adding
        # a little more to the rich side.
        lot = self._yes if opposite_side == "long_above" else self._no
        return lot.qty > 0 and lot.avg_price < self.config.seed_limit_price

    def _within_caps(self, side: str, contracts: float) -> bool:
        return self._clamp_contracts_to_capacity(side, contracts) >= contracts

    def _clamp_contracts_to_capacity(self, side: str, contracts: float) -> float:
        contracts = max(0.0, float(contracts))
        if contracts <= 0:
            return 0.0
        gross_room = max(0.0, self.config.max_gross_contracts - self.gross_contracts)
        if side == "long_above":
            net_room = max(0.0, self.config.max_net_contracts - (self._yes.qty - self._no.qty))
        else:
            net_room = max(0.0, self.config.max_net_contracts - (self._no.qty - self._yes.qty))
        return min(contracts, gross_room, net_room)

    def _cap_fill_to_inventory_limits(self, fill: PaperFill) -> PaperFill | None:
        allowed_contracts = self._clamp_contracts_to_capacity(fill.side, fill.contracts)
        if allowed_contracts <= 0:
            return None
        if allowed_contracts >= fill.contracts:
            return fill
        return replace(
            fill,
            contracts=allowed_contracts,
            notional=round(allowed_contracts * fill.entry_price, 6),
        )

    def _net_is_within_caps(self) -> bool:
        return abs(self._yes.qty - self._no.qty) <= self.config.max_net_contracts

    @property
    def gross_contracts(self) -> float:
        return self._yes.qty + self._no.qty

    def _features(self, state: MarketState, *, limit_price: float, contracts: float) -> dict[str, float | bool | str | None]:
        yes_mark = state.orderbook.yes_bid or 0.0
        no_mark = state.orderbook.no_bid or 0.0
        return {
            "limit_price": limit_price,
            "target_contracts": contracts,
            "yes_qty": self._yes.qty,
            "yes_avg_price": self._yes.avg_price,
            "no_qty": self._no.qty,
            "no_avg_price": self._no.avg_price,
            "raw_net_contracts": self._yes.qty - self._no.qty,
            "effective_mtm_exposure": self._yes.qty * yes_mark - self._no.qty * no_mark,
            "gross_contracts": self.gross_contracts,
            "combined_basis": self._yes.avg_price + self._no.avg_price if self._yes.qty > 0 and self._no.qty > 0 else None,
            "seconds_to_close": state.seconds_to_close,
        }

    def _record_decision(self, state: MarketState, signal: Signal, *, limit_price: float | None, contracts: float) -> None:
        self.decision_history.append(
            {
                "ts": state.tick.ts.isoformat(),
                "market_ticker": state.contract.ticker,
                "side": signal.side,
                "reason": signal.reason,
                "limit_price": limit_price,
                "target_contracts": contracts,
                "target_notional": signal.target_notional,
            }
        )

    def _record_snapshot(self, state: MarketState) -> None:
        yes_mark = state.orderbook.yes_bid or 0.0
        no_mark = state.orderbook.no_bid or 0.0
        self.position_history.append(
            SimpleInventorySnapshot(
                ts=state.tick.ts.isoformat(),
                market_ticker=state.contract.ticker,
                yes_qty=self._yes.qty,
                yes_avg_price=self._yes.avg_price,
                no_qty=self._no.qty,
                no_avg_price=self._no.avg_price,
                raw_net_contracts=self._yes.qty - self._no.qty,
                effective_mtm_exposure=self._yes.qty * yes_mark - self._no.qty * no_mark,
                gross_contracts=self.gross_contracts,
                combined_basis=self._yes.avg_price + self._no.avg_price if self._yes.qty > 0 and self._no.qty > 0 else None,
                yes_ask=state.orderbook.yes_ask,
                no_ask=state.orderbook.no_ask,
            )
        )
