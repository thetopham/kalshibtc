from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

InventorySide = Literal["ABOVE", "BELOW"]


@dataclass(frozen=True)
class InventoryVolRebalanceConfig:
    starter_qty_per_side: float = 5.0
    add_qty: float = 5.0
    cheap_add_threshold: float = 0.35
    deep_cheap_threshold: float = 0.20
    rebound_reduce_threshold: float = 0.30
    max_inventory_per_side: float = 250.0
    max_inventory_ratio: float = 3.0
    stop_new_adds_seconds_to_expiry: float = 45.0
    volatility_add_multiplier: bool = True
    post_impulse_cooldown_seconds: float = 10.0
    impulse_velocity_threshold: float = 5.0
    slippage: float = 0.0
    fee_per_contract: float = 0.0


@dataclass(frozen=True)
class InventoryVolFill:
    event_key: str
    market_ticker: str
    side: InventorySide
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
class InventoryVolReduction:
    event_key: str
    market_ticker: str
    side: InventorySide
    price: float
    quantity: float
    realized_pnl: float
    ts: datetime
    reason: str
    source_add_ts: str | None = None

    @property
    def proceeds(self) -> float:
        return round(self.price * self.quantity, 10)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_key": self.event_key,
            "market_ticker": self.market_ticker,
            "side": self.side,
            "price": self.price,
            "quantity": self.quantity,
            "proceeds": self.proceeds,
            "realized_pnl": self.realized_pnl,
            "ts": self.ts.isoformat(),
            "reason": self.reason,
            "source_add_ts": self.source_add_ts,
        }


@dataclass(frozen=True)
class InventoryVolDecision:
    fills: int
    reductions: int
    reason: str
    fill_events: list[InventoryVolFill] = field(default_factory=list)
    reduction_events: list[InventoryVolReduction] = field(default_factory=list)


@dataclass
class InventoryVolPosition:
    event_key: str
    market_ticker: str
    market_close_time: str
    strike: float
    held_above_qty: float = 0.0
    held_below_qty: float = 0.0
    above_cost: float = 0.0
    below_cost: float = 0.0
    realized_pnl: float = 0.0
    max_drawdown: float = 0.0
    peak_equity: float = 0.0
    last_mtm_equity: float = 0.0
    last_seconds_to_close: float = 0.0
    last_distance_from_strike: float = 0.0
    fills: list[InventoryVolFill] = field(default_factory=list)
    reductions: list[InventoryVolReduction] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    exposure_over_time: list[dict[str, Any]] = field(default_factory=list)

    @property
    def held_above_avg(self) -> float:
        return round(self.above_cost / self.held_above_qty, 10) if self.held_above_qty else 0.0

    @property
    def held_below_avg(self) -> float:
        return round(self.below_cost / self.held_below_qty, 10) if self.held_below_qty else 0.0

    @property
    def total_cost(self) -> float:
        return round(self.above_cost + self.below_cost, 10)

    @property
    def total_qty(self) -> float:
        return self.held_above_qty + self.held_below_qty

    @property
    def blended_basis(self) -> float | None:
        if not self.held_above_qty or not self.held_below_qty:
            return None
        return round(self.held_above_avg + self.held_below_avg, 10)

    @property
    def inventory_imbalance_ratio(self) -> float:
        smaller = min(self.held_above_qty, self.held_below_qty)
        larger = max(self.held_above_qty, self.held_below_qty)
        if smaller <= 0:
            return float("inf") if larger > 0 else 1.0
        return round(larger / smaller, 10)

    @property
    def inventory_imbalance_qty(self) -> float:
        return abs(self.held_above_qty - self.held_below_qty)

    def mark_to_market_equity(self, *, above_bid: float | None, below_bid: float | None) -> float | None:
        if above_bid is None or below_bid is None:
            return None
        return round(self.held_above_qty * above_bid + self.held_below_qty * below_bid + self.realized_pnl - self.total_cost, 10)

    def unrealized_pnl(self, *, above_bid: float | None, below_bid: float | None) -> float | None:
        if above_bid is None or below_bid is None:
            return None
        return round(self.held_above_qty * above_bid + self.held_below_qty * below_bid - self.total_cost, 10)

    def add_fill(self, *, side: InventorySide, price: float, quantity: float, fee: float, ts: datetime, reason: str) -> InventoryVolFill:
        fill = InventoryVolFill(self.event_key, self.market_ticker, side, price, quantity, fee, ts, reason)
        if side == "ABOVE":
            self.held_above_qty += quantity
            self.above_cost += fill.cost
        else:
            self.held_below_qty += quantity
            self.below_cost += fill.cost
        self.fills.append(fill)
        return fill

    def reduce(self, *, side: InventorySide, price: float, quantity: float, ts: datetime, reason: str) -> InventoryVolReduction:
        if side == "ABOVE":
            qty = min(quantity, self.held_above_qty)
            avg = self.held_above_avg
            self.held_above_qty -= qty
            self.above_cost -= avg * qty
        else:
            qty = min(quantity, self.held_below_qty)
            avg = self.held_below_avg
            self.held_below_qty -= qty
            self.below_cost -= avg * qty
        pnl = round((price - avg) * qty, 10)
        self.realized_pnl = round(self.realized_pnl + pnl, 10)
        source = self._latest_add_ts_for_side(side)
        reduction = InventoryVolReduction(self.event_key, self.market_ticker, side, price, qty, pnl, ts, reason, source)
        self.reductions.append(reduction)
        return reduction

    def record_mark(self, *, ts: datetime, above_bid: float | None, below_bid: float | None, btc_velocity_30s: float | None, distance_from_strike: float, atr_expansion: float | None = None, macd_histogram: float | None = None) -> None:
        equity = self.mark_to_market_equity(above_bid=above_bid, below_bid=below_bid)
        if equity is None:
            return
        self.last_mtm_equity = equity
        self.peak_equity = max(self.peak_equity, equity)
        drawdown = equity - self.peak_equity
        self.max_drawdown = min(self.max_drawdown, drawdown)
        point = {
            "ts": ts.isoformat(),
            "mtm_equity": equity,
            "unrealized_pnl": self.unrealized_pnl(above_bid=above_bid, below_bid=below_bid),
            "realized_pnl": self.realized_pnl,
            "blended_basis": self.blended_basis,
            "above_qty": self.held_above_qty,
            "below_qty": self.held_below_qty,
            "inventory_imbalance_ratio": self.inventory_imbalance_ratio,
            "btc_velocity_30s": btc_velocity_30s,
            "distance_from_strike": distance_from_strike,
            "atr_expansion": atr_expansion,
            "macd_histogram": macd_histogram,
        }
        self.equity_curve.append(point)
        self.exposure_over_time.append(point)

    def projected_ratio_for(self, side: InventorySide, quantity: float) -> float:
        above = self.held_above_qty + (quantity if side == "ABOVE" else 0.0)
        below = self.held_below_qty + (quantity if side == "BELOW" else 0.0)
        smaller = min(above, below)
        larger = max(above, below)
        if smaller <= 0:
            return float("inf")
        return larger / smaller

    def side_qty(self, side: InventorySide) -> float:
        return self.held_above_qty if side == "ABOVE" else self.held_below_qty

    def side_avg(self, side: InventorySide) -> float:
        return self.held_above_avg if side == "ABOVE" else self.held_below_avg

    def _latest_add_ts_for_side(self, side: InventorySide) -> str | None:
        for fill in reversed(self.fills):
            if fill.side == side and fill.reason != "starter_inventory_both_sides":
                return fill.ts.isoformat()
        return None

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
            "blended_basis": self.blended_basis,
            "total_cost": self.total_cost,
            "mark_to_market_equity": self.last_mtm_equity,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.equity_curve[-1].get("unrealized_pnl") if self.equity_curve else None,
            "inventory_imbalance_ratio": self.inventory_imbalance_ratio,
            "inventory_imbalance_qty": self.inventory_imbalance_qty,
            "max_drawdown": self.max_drawdown,
            "time_to_expiry": self.last_seconds_to_close,
            "distance_from_strike": self.last_distance_from_strike,
            "fills_json": [fill.as_dict() for fill in self.fills],
            "reductions_json": [reduction.as_dict() for reduction in self.reductions],
            "equity_curve_json": self.equity_curve,
            "exposure_over_time_json": self.exposure_over_time,
        }


class InventoryVolRebalanceManager:
    def __init__(self, config: InventoryVolRebalanceConfig | None = None) -> None:
        self.config = config or InventoryVolRebalanceConfig()
        self.positions: dict[str, InventoryVolPosition] = {}
        self.events: list[dict[str, Any]] = []
        self.add_events: list[dict[str, Any]] = []
        self.reversion_events: list[dict[str, Any]] = []

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
        btc_price: float,
        btc_velocity_30s: float | None,
        above_bid: float | None,
        above_ask: float | None,
        below_bid: float | None,
        below_ask: float | None,
        atr_expansion: float | None = None,
        macd_histogram: float | None = None,
    ) -> InventoryVolDecision:
        if above_ask is None or below_ask is None or above_bid is None or below_bid is None or min(above_ask, below_ask, above_bid, below_bid) < 0:
            self._log(event_key, market_ticker, ts, "NONE", "invalid_book")
            return InventoryVolDecision(0, 0, "invalid_book")
        position = self.positions.setdefault(
            event_key,
            InventoryVolPosition(event_key=event_key, market_ticker=market_ticker, market_close_time=market_close_time, strike=strike),
        )
        position.last_seconds_to_close = seconds_to_close
        position.last_distance_from_strike = distance_from_strike
        if not position.held_above_qty and not position.held_below_qty:
            decision = self._starter(position, ts, above_ask, below_ask)
        else:
            decision = self._reduce_or_add(
                position=position,
                ts=ts,
                seconds_to_close=seconds_to_close,
                distance_from_strike=distance_from_strike,
                btc_price=btc_price,
                btc_velocity_30s=btc_velocity_30s,
                above_bid=above_bid,
                above_ask=above_ask,
                below_bid=below_bid,
                below_ask=below_ask,
                atr_expansion=atr_expansion,
                macd_histogram=macd_histogram,
            )
        position.record_mark(ts=ts, above_bid=above_bid, below_bid=below_bid, btc_velocity_30s=btc_velocity_30s, distance_from_strike=distance_from_strike, atr_expansion=atr_expansion, macd_histogram=macd_histogram)
        return decision

    def _starter(self, position: InventoryVolPosition, ts: datetime, above_ask: float, below_ask: float) -> InventoryVolDecision:
        fills = [
            position.add_fill(side="ABOVE", price=round(above_ask + self.config.slippage, 10), quantity=self.config.starter_qty_per_side, fee=self.config.starter_qty_per_side * self.config.fee_per_contract, ts=ts, reason="starter_inventory_both_sides"),
            position.add_fill(side="BELOW", price=round(below_ask + self.config.slippage, 10), quantity=self.config.starter_qty_per_side, fee=self.config.starter_qty_per_side * self.config.fee_per_contract, ts=ts, reason="starter_inventory_both_sides"),
        ]
        for fill in fills:
            self._log(position.event_key, position.market_ticker, ts, fill.side, fill.reason, fill=fill)
        return InventoryVolDecision(2, 0, "starter_inventory_both_sides", fills)

    def _reduce_or_add(self, *, position: InventoryVolPosition, ts: datetime, seconds_to_close: float, distance_from_strike: float, btc_price: float, btc_velocity_30s: float | None, above_bid: float, above_ask: float, below_bid: float, below_ask: float, atr_expansion: float | None, macd_histogram: float | None) -> InventoryVolDecision:
        reduction = self._try_reduce(position, ts, above_bid, below_bid)
        if reduction is not None:
            self._log(position.event_key, position.market_ticker, ts, reduction.side, reduction.reason, reduction=reduction)
            self._record_reversion(position, reduction, ts)
            return InventoryVolDecision(0, 1, "reduce_rebounded_overweight_inventory", reduction_events=[reduction])
        if seconds_to_close <= self.config.stop_new_adds_seconds_to_expiry:
            self._log(position.event_key, position.market_ticker, ts, "NONE", "stop_new_adds_near_expiry")
            return InventoryVolDecision(0, 0, "stop_new_adds_near_expiry")
        candidate = self._crushed_side(above_ask=above_ask, below_ask=below_ask, distance_from_strike=distance_from_strike)
        if candidate is None:
            self._log(position.event_key, position.market_ticker, ts, "NONE", "no_crushed_side")
            return InventoryVolDecision(0, 0, "no_crushed_side")
        side, ask = candidate
        quantity = self._add_quantity(ask=ask, btc_velocity_30s=btc_velocity_30s)
        if position.side_qty(side) + quantity > self.config.max_inventory_per_side:
            self._log(position.event_key, position.market_ticker, ts, side, "max_inventory_per_side")
            return InventoryVolDecision(0, 0, "max_inventory_per_side")
        if position.projected_ratio_for(side, quantity) > self.config.max_inventory_ratio:
            self._log(position.event_key, position.market_ticker, ts, side, "max_inventory_ratio")
            return InventoryVolDecision(0, 0, "max_inventory_ratio")
        price = round(ask + self.config.slippage, 10)
        fill = position.add_fill(side=side, price=price, quantity=quantity, fee=quantity * self.config.fee_per_contract, ts=ts, reason="volatility_add_crushed_side")
        add_event = {
            "ts": ts.isoformat(),
            "event_key": position.event_key,
            "market_ticker": position.market_ticker,
            "side": side,
            "price": price,
            "quantity": quantity,
            "btc_price": btc_price,
            "btc_velocity_30s": btc_velocity_30s,
            "distance_from_strike": distance_from_strike,
            "atr_expansion": atr_expansion,
            "macd_histogram": macd_histogram,
            "seconds_to_close": position.last_seconds_to_close,
        }
        self.add_events.append(add_event)
        self._log(position.event_key, position.market_ticker, ts, side, fill.reason, fill=fill, research=add_event)
        return InventoryVolDecision(1, 0, "volatility_add_crushed_side", [fill])

    def _try_reduce(self, position: InventoryVolPosition, ts: datetime, above_bid: float, below_bid: float) -> InventoryVolReduction | None:
        if position.held_above_qty > position.held_below_qty and above_bid >= max(self.config.rebound_reduce_threshold, position.held_above_avg + 0.02):
            return position.reduce(side="ABOVE", price=above_bid, quantity=self.config.add_qty, ts=ts, reason="reduce_rebounded_overweight_inventory")
        if position.held_below_qty > position.held_above_qty and below_bid >= max(self.config.rebound_reduce_threshold, position.held_below_avg + 0.02):
            return position.reduce(side="BELOW", price=below_bid, quantity=self.config.add_qty, ts=ts, reason="reduce_rebounded_overweight_inventory")
        return None

    def _crushed_side(self, *, above_ask: float, below_ask: float, distance_from_strike: float) -> tuple[InventorySide, float] | None:
        candidates: list[tuple[InventorySide, float]] = []
        if below_ask <= self.config.cheap_add_threshold and distance_from_strike > 0:
            candidates.append(("BELOW", below_ask))
        if above_ask <= self.config.cheap_add_threshold and distance_from_strike < 0:
            candidates.append(("ABOVE", above_ask))
        if not candidates:
            if below_ask <= self.config.deep_cheap_threshold:
                candidates.append(("BELOW", below_ask))
            if above_ask <= self.config.deep_cheap_threshold:
                candidates.append(("ABOVE", above_ask))
        return min(candidates, key=lambda item: item[1]) if candidates else None

    def _add_quantity(self, *, ask: float, btc_velocity_30s: float | None) -> float:
        qty = self.config.add_qty
        if self.config.volatility_add_multiplier and ask <= self.config.deep_cheap_threshold and abs(btc_velocity_30s or 0.0) >= self.config.impulse_velocity_threshold:
            qty *= 2
        return qty

    def _record_reversion(self, position: InventoryVolPosition, reduction: InventoryVolReduction, ts: datetime) -> None:
        if not reduction.source_add_ts:
            return
        try:
            source_ts = datetime.fromisoformat(reduction.source_add_ts)
        except ValueError:
            return
        self.reversion_events.append({
            "event_key": position.event_key,
            "market_ticker": position.market_ticker,
            "side": reduction.side,
            "source_add_ts": reduction.source_add_ts,
            "reversion_ts": ts.isoformat(),
            "seconds_to_reversion": round((ts - source_ts).total_seconds(), 10),
            "realized_pnl": reduction.realized_pnl,
        })

    def _log(self, event_key: str, market_ticker: str, ts: datetime, side: str, reason: str, fill: InventoryVolFill | None = None, reduction: InventoryVolReduction | None = None, research: dict[str, Any] | None = None) -> None:
        self.events.append({
            "event_key": event_key,
            "market_ticker": market_ticker,
            "ts": ts.isoformat(),
            "side": side,
            "quantity": fill.quantity if fill else reduction.quantity if reduction else 0.0,
            "price": fill.price if fill else reduction.price if reduction else None,
            "event_type": "add" if fill else "reduction" if reduction else "scan",
            "reason": reason,
            "raw_json": {"fill": fill.as_dict() if fill else None, "reduction": reduction.as_dict() if reduction else None, "research": research},
        })

    def research_metrics(self) -> dict[str, Any]:
        positions = list(self.positions.values())
        equity_curve = [point for position in positions for point in position.equity_curve]
        return {
            "add_events_vs_btc_volatility_spikes": self.add_events,
            "add_events_vs_distance_from_strike": [{"ts": e["ts"], "side": e["side"], "distance_from_strike": e["distance_from_strike"], "price": e["price"]} for e in self.add_events],
            "add_events_vs_atr_expansion": [{"ts": e["ts"], "atr_expansion": e.get("atr_expansion"), "side": e["side"]} for e in self.add_events],
            "add_events_vs_macd_histogram_inflection": [{"ts": e["ts"], "macd_histogram": e.get("macd_histogram"), "side": e["side"]} for e in self.add_events],
            "time_to_reversion_after_add": self.reversion_events,
            "equity_curve_over_intraperiod_oscillations": equity_curve,
            "realized_volatility_harvested_per_cycle": round(sum(event["realized_pnl"] for event in self.reversion_events), 10),
            "exposure_over_time": [point for position in positions for point in position.exposure_over_time],
        }
