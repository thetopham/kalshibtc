from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from ..execution.paper import PaperFill
from ..market.pricing import entry_price_for_signal
from ..market.state import MarketState
from .signals import Signal


@dataclass(frozen=True)
class ComplementLadderConfig:
    """Simple YES/NO complement-basis ladder.

    The hard invariant is lot-level pairing: a filled YES lot may only be
    completed with NO at ``target_pair_cost - yes_fill_price`` or better, and
    vice versa. This keeps completed pairs at or below the target cost instead
    of relying on blended averages that can hide bad pairs.
    """

    name: str = "complement_ladder_v0"
    target_pair_cost: float = 0.95
    edge_buffer: float = 0.0
    direct_pair_contracts: float = 20.0
    cheap_add_contracts: float = 1.0
    cheap_threshold: float = 0.15
    extreme_cheap_threshold: float = 0.08
    extreme_cheap_contracts: float = 2.0
    max_unpaired_contracts: float = 10.0
    max_pairs_per_market: float = 100.0
    min_seconds_to_open_unpaired: float = 240.0
    completion_only_seconds: float = 240.0
    no_trade_seconds: float = 15.0
    min_seconds_between_orders: float = 0.0
    min_confidence: float = 0.70

    @property
    def effective_target_pair_cost(self) -> float:
        return self.target_pair_cost - self.edge_buffer


@dataclass
class OpenComplementLot:
    side: str
    qty: float
    price: float
    ts: datetime
    required_opposite_price: float


@dataclass(frozen=True)
class ComplementLadderSnapshot:
    ts: str
    market_ticker: str
    yes_qty: float
    no_qty: float
    paired_qty: float
    unpaired_yes_qty: float
    unpaired_no_qty: float
    paired_avg_cost: float | None
    locked_edge_per_pair: float | None
    open_lots: int
    yes_ask: float | None
    no_ask: float | None
    seconds_to_close: float


@dataclass
class ComplementLadderV0Strategy:
    """Paper/replay strategy for accumulating YES/NO pairs below a hard cost.

    It intentionally ignores directional prediction. It first completes already
    filled one-sided lots at their lot-specific required opposite price, then
    takes direct YES+NO opportunities, then opens small one-sided cheap lots only
    when enough time remains and unpaired exposure is capped.
    """

    config: ComplementLadderConfig = field(default_factory=ComplementLadderConfig)

    def __post_init__(self) -> None:
        self._active_market_ticker: str | None = None
        self._open_lots: list[OpenComplementLot] = []
        self._paired_yes_cost = 0.0
        self._paired_no_cost = 0.0
        self._paired_qty = 0.0
        self._pending_role: str | None = None
        self._pending_lot_index: int | None = None
        self._pending_direct_pair_side: str | None = None
        self._direct_pair_pending: dict[str, float | None] | None = None
        self._last_order_ts: datetime | None = None
        self.position_history: list[ComplementLadderSnapshot] = []
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
        if fill.side not in {"long_above", "long_below"}:
            return
        side = "yes" if fill.side == "long_above" else "no"
        qty = float(fill.contracts)
        price = float(fill.entry_price)
        if qty <= 0 or price <= 0:
            return

        if self._pending_role == "complete" and self._pending_lot_index is not None:
            self._complete_lot(side=side, qty=qty, price=price)
        elif self._pending_role == "direct_pair":
            self._record_direct_pair_fill(side=side, qty=qty, price=price, ts=state.tick.ts)
        else:
            self._add_open_lot(side=side, qty=qty, price=price, ts=state.tick.ts)

        self._pending_role = None
        self._pending_lot_index = None
        self._last_order_ts = state.tick.ts
        self._record_snapshot(state)

    def _reset(self, market_ticker: str) -> None:
        self._active_market_ticker = market_ticker
        self._open_lots = []
        self._paired_yes_cost = 0.0
        self._paired_no_cost = 0.0
        self._paired_qty = 0.0
        self._pending_role = None
        self._pending_lot_index = None
        self._pending_direct_pair_side = None
        self._direct_pair_pending = None
        self._last_order_ts = None

    def _choose_signal(self, state: MarketState) -> Signal:
        self._pending_role = None
        self._pending_lot_index = None
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

        completion = self._completion_signal(state, yes_ask=yes_ask, no_ask=no_ask)
        if completion is not None:
            return completion

        direct = self._direct_pair_signal(state, yes_ask=yes_ask, no_ask=no_ask)
        if direct is not None:
            return direct

        if state.seconds_to_close <= self.config.completion_only_seconds:
            return self._none("completion-only window; no new unpaired lots")
        if state.seconds_to_close < self.config.min_seconds_to_open_unpaired:
            return self._none("too close to expiry for new unpaired lot")

        return self._cheap_unpaired_signal(state, yes_ask=yes_ask, no_ask=no_ask)

    def _completion_signal(self, state: MarketState, *, yes_ask: float, no_ask: float) -> Signal | None:
        candidates: list[tuple[float, int, OpenComplementLot, str, float]] = []
        for idx, lot in enumerate(self._open_lots):
            opposite_side = "no" if lot.side == "yes" else "yes"
            ask = no_ask if opposite_side == "no" else yes_ask
            if ask <= lot.required_opposite_price:
                edge = self.config.effective_target_pair_cost - (lot.price + ask)
                candidates.append((edge, idx, lot, opposite_side, ask))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[2].qty), reverse=True)
        _edge, idx, lot, side, ask = candidates[0]
        qty = min(lot.qty, self._remaining_pair_capacity())
        if qty <= 0:
            return self._none("max pairs per market reached")
        self._pending_role = "complete"
        self._pending_lot_index = idx
        return self._signal(
            side=side,
            reason="complete open complement lot below target pair cost",
            contracts=qty,
            price=ask,
            features={
                "limit_price": lot.required_opposite_price,
                "lot_price": lot.price,
                "required_opposite_price": lot.required_opposite_price,
                "projected_pair_cost": lot.price + ask,
                "target_pair_cost": self.config.effective_target_pair_cost,
                "seconds_to_close": state.seconds_to_close,
            },
        )

    def _direct_pair_signal(self, state: MarketState, *, yes_ask: float, no_ask: float) -> Signal | None:
        pair_cost = yes_ask + no_ask
        if pair_cost > self.config.effective_target_pair_cost:
            return None
        qty = min(
            self.config.direct_pair_contracts,
            self._remaining_pair_capacity(),
        )
        if qty <= 0:
            return self._none("direct pair blocked by capacity")
        if self._pending_direct_pair_side == "yes":
            side = "no"
            price = no_ask
        else:
            side = "yes"
            price = yes_ask
        self._pending_role = "direct_pair"
        return self._signal(
            side=side,
            reason="direct complement pair below target cost",
            contracts=qty,
            price=price,
            features={
                "limit_price": price,
                "pair_cost": pair_cost,
                "target_pair_cost": self.config.effective_target_pair_cost,
                "paired_qty": self._paired_qty,
                "seconds_to_close": state.seconds_to_close,
            },
        )

    def _cheap_unpaired_signal(self, state: MarketState, *, yes_ask: float, no_ask: float) -> Signal:
        cheap_side = "yes" if yes_ask <= no_ask else "no"
        cheap_price = yes_ask if cheap_side == "yes" else no_ask
        if cheap_price > self.config.cheap_threshold:
            return self._none("no direct pair and cheap side above threshold")
        if self.unpaired_qty >= self.config.max_unpaired_contracts:
            return self._none("max unpaired exposure reached")
        contracts = self.config.extreme_cheap_contracts if cheap_price <= self.config.extreme_cheap_threshold else self.config.cheap_add_contracts
        contracts = min(contracts, self._remaining_unpaired_capacity())
        if contracts <= 0:
            return self._none("no unpaired capacity remaining")
        required = self.config.effective_target_pair_cost - cheap_price
        if required <= 0:
            return self._none("cheap side leaves no valid opposite completion price")
        self._pending_role = "open"
        return self._signal(
            side=cheap_side,
            reason="open cheap complement lot with target completion limit",
            contracts=contracts,
            price=cheap_price,
            features={
                "limit_price": cheap_price,
                "fill_price": cheap_price,
                "required_opposite_price": required,
                "target_pair_cost": self.config.effective_target_pair_cost,
                "unpaired_qty": self.unpaired_qty,
                "seconds_to_close": state.seconds_to_close,
            },
        )

    def _signal(self, *, side: str, reason: str, contracts: float, price: float, features: dict[str, float | bool | str | None]) -> Signal:
        paper_side = "long_above" if side == "yes" else "long_below"
        return Signal(
            paper_side,
            reason,
            self.config.min_confidence,
            strategy=self.name,
            target_notional=round(contracts * price, 6),
            estimated_shares=contracts,
            features=features,
            allow_price_strike_mismatch=True,
        )

    def _none(self, reason: str) -> Signal:
        return Signal("none", reason, 0.0, strategy=self.name, allow_price_strike_mismatch=True)

    def _add_open_lot(self, *, side: str, qty: float, price: float, ts: datetime) -> None:
        self._open_lots.append(
            OpenComplementLot(
                side=side,
                qty=qty,
                price=price,
                ts=ts,
                required_opposite_price=self.config.effective_target_pair_cost - price,
            )
        )

    def _complete_lot(self, *, side: str, qty: float, price: float) -> None:
        idx = self._pending_lot_index
        if idx is None or idx >= len(self._open_lots):
            return
        lot = self._open_lots[idx]
        fill_qty = min(qty, lot.qty, self._remaining_pair_capacity())
        if fill_qty <= 0:
            return
        if lot.side == "yes" and side == "no":
            self._paired_yes_cost += fill_qty * lot.price
            self._paired_no_cost += fill_qty * price
        elif lot.side == "no" and side == "yes":
            self._paired_yes_cost += fill_qty * price
            self._paired_no_cost += fill_qty * lot.price
        else:
            return
        self._paired_qty += fill_qty
        lot.qty -= fill_qty
        if lot.qty <= 1e-9:
            self._open_lots.pop(idx)

    def _record_direct_pair_fill(self, *, side: str, qty: float, price: float, ts: datetime) -> None:
        if self._direct_pair_pending is None:
            self._direct_pair_pending = {"yes_qty": 0.0, "yes_price": None, "no_qty": 0.0, "no_price": None}
        if side == "yes":
            self._direct_pair_pending["yes_qty"] = float(self._direct_pair_pending["yes_qty"] or 0.0) + qty
            self._direct_pair_pending["yes_price"] = price
            self._pending_direct_pair_side = "yes"
        else:
            self._direct_pair_pending["no_qty"] = float(self._direct_pair_pending["no_qty"] or 0.0) + qty
            self._direct_pair_pending["no_price"] = price
            self._pending_direct_pair_side = "no"
        yes_qty = float(self._direct_pair_pending["yes_qty"] or 0.0)
        no_qty = float(self._direct_pair_pending["no_qty"] or 0.0)
        pair_qty = min(yes_qty, no_qty, self._remaining_pair_capacity())
        if pair_qty <= 0:
            return
        yes_price = self._direct_pair_pending["yes_price"]
        no_price = self._direct_pair_pending["no_price"]
        if yes_price is None or no_price is None:
            return
        self._paired_yes_cost += pair_qty * float(yes_price)
        self._paired_no_cost += pair_qty * float(no_price)
        self._paired_qty += pair_qty
        self._direct_pair_pending["yes_qty"] = yes_qty - pair_qty
        self._direct_pair_pending["no_qty"] = no_qty - pair_qty
        if self._direct_pair_pending["yes_qty"] <= 1e-9 and self._direct_pair_pending["no_qty"] <= 1e-9:
            self._direct_pair_pending = None
            self._pending_direct_pair_side = None

    def _remaining_pair_capacity(self) -> float:
        return max(0.0, self.config.max_pairs_per_market - self._paired_qty)

    def _remaining_unpaired_capacity(self) -> float:
        return max(0.0, self.config.max_unpaired_contracts - self.unpaired_qty)

    @property
    def unpaired_qty(self) -> float:
        pending_direct = 0.0
        if self._direct_pair_pending is not None:
            pending_direct = abs(float(self._direct_pair_pending["yes_qty"] or 0.0) - float(self._direct_pair_pending["no_qty"] or 0.0))
        return sum(lot.qty for lot in self._open_lots) + pending_direct

    @property
    def paired_avg_cost(self) -> float | None:
        if self._paired_qty <= 0:
            return None
        return (self._paired_yes_cost + self._paired_no_cost) / self._paired_qty

    @property
    def locked_edge_per_pair(self) -> float | None:
        avg = self.paired_avg_cost
        if avg is None:
            return None
        return 1.0 - avg

    def _record_decision(self, state: MarketState, signal: Signal) -> None:
        self.decision_history.append(
            {
                "ts": state.tick.ts.isoformat(),
                "market_ticker": state.contract.ticker,
                "side": signal.side,
                "reason": signal.reason,
                "target_notional": signal.target_notional,
                "estimated_shares": signal.estimated_shares,
                "paired_avg_cost": self.paired_avg_cost,
                "unpaired_qty": self.unpaired_qty,
            }
        )

    def _record_snapshot(self, state: MarketState) -> None:
        yes_unpaired = sum(lot.qty for lot in self._open_lots if lot.side == "yes")
        no_unpaired = sum(lot.qty for lot in self._open_lots if lot.side == "no")
        if self._direct_pair_pending is not None:
            yes_unpaired += float(self._direct_pair_pending["yes_qty"] or 0.0)
            no_unpaired += float(self._direct_pair_pending["no_qty"] or 0.0)
        self.position_history.append(
            ComplementLadderSnapshot(
                ts=state.tick.ts.isoformat(),
                market_ticker=state.contract.ticker,
                yes_qty=self._paired_qty + yes_unpaired,
                no_qty=self._paired_qty + no_unpaired,
                paired_qty=self._paired_qty,
                unpaired_yes_qty=yes_unpaired,
                unpaired_no_qty=no_unpaired,
                paired_avg_cost=self.paired_avg_cost,
                locked_edge_per_pair=self.locked_edge_per_pair,
                open_lots=len(self._open_lots),
                yes_ask=state.orderbook.yes_ask,
                no_ask=state.orderbook.no_ask,
                seconds_to_close=state.seconds_to_close,
            )
        )
