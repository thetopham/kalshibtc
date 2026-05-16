from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean
from typing import Any, Literal

VolRegimeSide = Literal["ABOVE", "BELOW"]


@dataclass(frozen=True)
class InventoryVolRegimeConfig:
    base_notional: float = 25.0
    starter_unit_notional: float = 25.0
    starter_trend_ratio: float = 3.0
    starter_countertrend_ratio: float = 2.0
    target_combined_basis: float = 0.99
    min_basis_improvement: float = 0.005
    max_notional_per_add: float = 250.0
    floor_price: float = 0.05
    max_price: float = 0.90
    max_total_cost_per_market: float = 2_500.0
    max_qty_per_side: float = 5_000.0
    starter_max_seconds_to_close: float = 14.0 * 60.0
    starter_min_seconds_to_close: float = 11.0 * 60.0
    momentum_min_seconds_to_close: float = 8.0 * 60.0
    compression_seconds_to_close: float = 5.0 * 60.0
    atr_baseline: float = 20.0
    atr_expansion_threshold: float = 1.4
    distance_velocity_threshold: float = 2.0
    stabilization_velocity_ratio: float = 0.55
    compression_crushed_price: float = 0.35
    deep_crushed_price: float = 0.22
    cooldown_seconds: float = 15.0
    slippage: float = 0.01
    fee_per_contract: float = 0.0


@dataclass(frozen=True)
class VolRegimeFeatures:
    atr_1m: float
    atr_expansion_rate: float
    distance_from_strike: float
    velocity_away_from_strike: float
    velocity_slowdown: float
    macd_histogram: float
    macd_slope: float
    time_to_expiry_seconds: float
    volatility_regime_score: float
    expansion_regime: bool
    stabilization_regime: bool
    compression_regime: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "atr_1m": self.atr_1m,
            "atr_expansion_rate": self.atr_expansion_rate,
            "distance_from_strike": self.distance_from_strike,
            "velocity_away_from_strike": self.velocity_away_from_strike,
            "velocity_slowdown": self.velocity_slowdown,
            "macd_histogram": self.macd_histogram,
            "macd_slope": self.macd_slope,
            "time_to_expiry_seconds": self.time_to_expiry_seconds,
            "volatility_regime_score": self.volatility_regime_score,
            "expansion_regime": self.expansion_regime,
            "stabilization_regime": self.stabilization_regime,
            "compression_regime": self.compression_regime,
        }


@dataclass(frozen=True)
class InventoryVolRegimeFill:
    event_key: str
    market_ticker: str
    side: VolRegimeSide
    price: float
    quantity: float
    notional: float
    fee: float
    ts: datetime
    reason: str
    features: dict[str, Any]

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
            "notional": self.notional,
            "fee": self.fee,
            "cost": self.cost,
            "ts": self.ts.isoformat(),
            "reason": self.reason,
            "features": self.features,
        }


@dataclass(frozen=True)
class InventoryVolRegimeDecision:
    fills: int
    reason: str
    fill_events: list[InventoryVolRegimeFill] = field(default_factory=list)
    features: VolRegimeFeatures | None = None


@dataclass
class InventoryVolRegimePosition:
    event_key: str
    market_ticker: str
    market_close_time: str
    strike: float
    held_above_qty: float = 0.0
    held_below_qty: float = 0.0
    above_cost: float = 0.0
    below_cost: float = 0.0
    last_mtm_equity: float = 0.0
    peak_equity: float = 0.0
    max_drawdown: float = 0.0
    last_seconds_to_close: float = 0.0
    last_distance_from_strike: float = 0.0
    fills: list[InventoryVolRegimeFill] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    features_timeline: list[dict[str, Any]] = field(default_factory=list)

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
    def inventory_imbalance_qty(self) -> float:
        return abs(self.held_above_qty - self.held_below_qty)

    @property
    def inventory_imbalance_ratio(self) -> float:
        smaller = min(self.held_above_qty, self.held_below_qty)
        larger = max(self.held_above_qty, self.held_below_qty)
        if smaller <= 0:
            return float("inf") if larger > 0 else 1.0
        return round(larger / smaller, 10)

    @property
    def blended_basis(self) -> float | None:
        if not self.held_above_qty or not self.held_below_qty:
            return None
        return round(self.held_above_avg + self.held_below_avg, 10)

    def side_qty(self, side: VolRegimeSide) -> float:
        return self.held_above_qty if side == "ABOVE" else self.held_below_qty

    def projected_blended_basis(self, *, side: VolRegimeSide, price: float, quantity: float) -> float | None:
        above_qty = self.held_above_qty + (quantity if side == "ABOVE" else 0.0)
        below_qty = self.held_below_qty + (quantity if side == "BELOW" else 0.0)
        if above_qty <= 0 or below_qty <= 0:
            return None
        above_cost = self.above_cost + (price * quantity if side == "ABOVE" else 0.0)
        below_cost = self.below_cost + (price * quantity if side == "BELOW" else 0.0)
        return round(above_cost / above_qty + below_cost / below_qty, 10)

    def add_fill(self, *, side: VolRegimeSide, price: float, quantity: float, fee: float, ts: datetime, reason: str, features: VolRegimeFeatures) -> InventoryVolRegimeFill:
        fill = InventoryVolRegimeFill(
            event_key=self.event_key,
            market_ticker=self.market_ticker,
            side=side,
            price=price,
            quantity=quantity,
            notional=round(price * quantity, 10),
            fee=fee,
            ts=ts,
            reason=reason,
            features=features.as_dict(),
        )
        if side == "ABOVE":
            self.held_above_qty += quantity
            self.above_cost += fill.cost
        else:
            self.held_below_qty += quantity
            self.below_cost += fill.cost
        self.fills.append(fill)
        return fill

    def mark_to_market_equity(self, *, above_bid: float | None, below_bid: float | None) -> float | None:
        if above_bid is None or below_bid is None:
            return None
        return round(self.held_above_qty * above_bid + self.held_below_qty * below_bid - self.total_cost, 10)

    def unrealized_pnl(self, *, above_bid: float | None, below_bid: float | None) -> float | None:
        return self.mark_to_market_equity(above_bid=above_bid, below_bid=below_bid)

    def record_mark(self, *, ts: datetime, above_bid: float | None, below_bid: float | None, features: VolRegimeFeatures) -> None:
        self.features_timeline.append({"ts": ts.isoformat(), **features.as_dict()})
        equity = self.mark_to_market_equity(above_bid=above_bid, below_bid=below_bid)
        if equity is None:
            return
        self.last_mtm_equity = equity
        self.peak_equity = max(self.peak_equity, equity)
        self.max_drawdown = min(self.max_drawdown, equity - self.peak_equity)
        self.equity_curve.append(
            {
                "ts": ts.isoformat(),
                "mtm_equity": equity,
                "unrealized_pnl": equity,
                "held_above_qty": self.held_above_qty,
                "held_below_qty": self.held_below_qty,
                "held_above_avg": self.held_above_avg,
                "held_below_avg": self.held_below_avg,
                "blended_basis": self.blended_basis,
                "inventory_imbalance_ratio": self.inventory_imbalance_ratio,
                "inventory_imbalance_qty": self.inventory_imbalance_qty,
                **features.as_dict(),
            }
        )

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
            "unrealized_pnl": self.equity_curve[-1]["unrealized_pnl"] if self.equity_curve else None,
            "inventory_imbalance_ratio": self.inventory_imbalance_ratio,
            "inventory_imbalance_qty": self.inventory_imbalance_qty,
            "max_drawdown": self.max_drawdown,
            "time_to_expiry": self.last_seconds_to_close,
            "distance_from_strike": self.last_distance_from_strike,
            "fills_json": [fill.as_dict() for fill in self.fills],
            "equity_curve_json": self.equity_curve,
            "features_timeline_json": self.features_timeline,
        }


class InventoryVolRegimeManager:
    def __init__(self, config: InventoryVolRegimeConfig | None = None) -> None:
        self.config = config or InventoryVolRegimeConfig()
        self.positions: dict[str, InventoryVolRegimePosition] = {}
        self.history: dict[str, list[dict[str, Any]]] = {}
        self.events: list[dict[str, Any]] = []
        self.fill_events: list[InventoryVolRegimeFill] = []
        self.last_fill_ts: dict[str, datetime] = {}

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
        atr_1m: float | None = None,
        atr_expansion_rate: float | None = None,
        macd_histogram: float | None = None,
        macd_slope: float | None = None,
    ) -> InventoryVolRegimeDecision:
        if not self._valid_book(above_bid=above_bid, above_ask=above_ask, below_bid=below_bid, below_ask=below_ask):
            self._log(event_key, market_ticker, ts, "NONE", "invalid_book", None, None)
            return InventoryVolRegimeDecision(0, "invalid_book")
        features = self._features(
            event_key=event_key,
            ts=ts,
            btc_price=btc_price,
            distance_from_strike=distance_from_strike,
            btc_velocity_30s=btc_velocity_30s,
            seconds_to_close=seconds_to_close,
            atr_1m=atr_1m,
            atr_expansion_rate=atr_expansion_rate,
            macd_histogram=macd_histogram,
            macd_slope=macd_slope,
        )
        position = self.positions.setdefault(
            event_key,
            InventoryVolRegimePosition(event_key=event_key, market_ticker=market_ticker, market_close_time=market_close_time, strike=strike),
        )
        position.last_seconds_to_close = seconds_to_close
        position.last_distance_from_strike = distance_from_strike
        fills: list[InventoryVolRegimeFill] = []
        reason = "scan_no_add"
        if self._cooldown_ok(event_key, ts):
            candidates = self._candidate_adds(position=position, features=features, above_ask=above_ask or 0.0, below_ask=below_ask or 0.0)
            for side, ask, add_reason, notional_override in candidates:
                quantity = self._dynamic_quantity(side=side, price=ask, features=features, notional_override=notional_override)
                if quantity <= 0:
                    continue
                fill_price = round(ask + self.config.slippage, 10)
                if not self._risk_ok(position=position, side=side, quantity=quantity, price=fill_price):
                    self._log(event_key, market_ticker, ts, side, "risk_limit", None, features)
                    continue
                if not self._basis_ok(position=position, side=side, quantity=quantity, price=fill_price, reason=add_reason, features=features):
                    self._log(event_key, market_ticker, ts, side, "combined_basis_not_improved", None, features)
                    continue
                fill = position.add_fill(
                    side=side,
                    price=fill_price,
                    quantity=quantity,
                    fee=quantity * self.config.fee_per_contract,
                    ts=ts,
                    reason=add_reason,
                    features=features,
                )
                fills.append(fill)
                self.fill_events.append(fill)
                self.last_fill_ts[event_key] = ts
                self._log(event_key, market_ticker, ts, side, add_reason, fill, features)
                reason = add_reason
                if not add_reason.startswith("starter_"):
                    break
        else:
            reason = "cooldown"
        position.record_mark(ts=ts, above_bid=above_bid, below_bid=below_bid, features=features)
        if not fills and reason == "scan_no_add":
            self._log(event_key, market_ticker, ts, "NONE", reason, None, features)
        return InventoryVolRegimeDecision(len(fills), reason, fills, features)

    def _valid_book(self, *, above_bid: float | None, above_ask: float | None, below_bid: float | None, below_ask: float | None) -> bool:
        values = [above_bid, above_ask, below_bid, below_ask]
        if any(value is None for value in values):
            return False
        assert above_bid is not None and above_ask is not None and below_bid is not None and below_ask is not None
        if min(above_bid, above_ask, below_bid, below_ask) <= 0 or max(above_bid, above_ask, below_bid, below_ask) > 1:
            return False
        return above_bid <= above_ask and below_bid <= below_ask and above_bid + below_bid <= 1.0

    def _features(self, *, event_key: str, ts: datetime, btc_price: float, distance_from_strike: float, btc_velocity_30s: float | None, seconds_to_close: float, atr_1m: float | None, atr_expansion_rate: float | None, macd_histogram: float | None, macd_slope: float | None) -> VolRegimeFeatures:
        history = self.history.setdefault(event_key, [])
        prior = history[-1] if history else None
        if atr_1m is None:
            recent = [abs(item["btc_price"] - prev["btc_price"]) for item, prev in zip(history[-59:], history[-60:-1], strict=False)]
            atr_1m = mean(recent) if recent else 0.0
        if atr_expansion_rate is None:
            baseline = mean([item["atr_1m"] for item in history[-180:] if item.get("atr_1m") is not None] or [self.config.atr_baseline])
            atr_expansion_rate = atr_1m / max(baseline, 1e-9)
        velocity = btc_velocity_30s if btc_velocity_30s is not None else 0.0
        direction = 1.0 if distance_from_strike >= 0 else -1.0
        velocity_away = max(0.0, velocity * direction)
        prior_velocity_away = float(prior["velocity_away_from_strike"]) if prior else velocity_away
        velocity_slowdown = max(0.0, prior_velocity_away - velocity_away)
        if macd_histogram is None:
            macd_histogram = velocity
        if macd_slope is None:
            macd_slope = macd_histogram - (float(prior["macd_histogram"]) if prior else macd_histogram)
        volatility_score = min(5.0, 0.7 * atr_expansion_rate + 0.015 * abs(distance_from_strike) + 0.2 * velocity_away)
        expansion = seconds_to_close > self.config.momentum_min_seconds_to_close and (
            atr_expansion_rate >= self.config.atr_expansion_threshold or velocity_away >= self.config.distance_velocity_threshold
        )
        stabilization = velocity_slowdown >= max(1.0, prior_velocity_away * self.config.stabilization_velocity_ratio)
        compression = seconds_to_close <= self.config.compression_seconds_to_close
        features = VolRegimeFeatures(
            atr_1m=round(float(atr_1m), 10),
            atr_expansion_rate=round(float(atr_expansion_rate), 10),
            distance_from_strike=round(distance_from_strike, 10),
            velocity_away_from_strike=round(velocity_away, 10),
            velocity_slowdown=round(velocity_slowdown, 10),
            macd_histogram=round(float(macd_histogram), 10),
            macd_slope=round(float(macd_slope), 10),
            time_to_expiry_seconds=round(seconds_to_close, 10),
            volatility_regime_score=round(volatility_score, 10),
            expansion_regime=expansion,
            stabilization_regime=stabilization,
            compression_regime=compression,
        )
        history.append({"ts": ts.isoformat(), "btc_price": btc_price, **features.as_dict()})
        if len(history) > 600:
            del history[:-600]
        return features

    def _candidate_adds(self, *, position: InventoryVolRegimePosition, features: VolRegimeFeatures, above_ask: float, below_ask: float) -> list[tuple[VolRegimeSide, float, str, float | None]]:
        candidates: list[tuple[VolRegimeSide, float, str, float | None]] = []
        momentum_side: VolRegimeSide = "ABOVE" if features.distance_from_strike > 0 else "BELOW"
        crushed_side: VolRegimeSide = "BELOW" if momentum_side == "ABOVE" else "ABOVE"
        momentum_ask = above_ask if momentum_side == "ABOVE" else below_ask
        crushed_ask = below_ask if crushed_side == "BELOW" else above_ask
        if self.config.starter_min_seconds_to_close <= features.time_to_expiry_seconds <= self.config.starter_max_seconds_to_close and position.total_cost == 0:
            candidates.append((momentum_side, momentum_ask, "starter_trend_leg_3x", self.config.starter_unit_notional * self.config.starter_trend_ratio))
            candidates.append((crushed_side, crushed_ask, "starter_countertrend_leg_2x", self.config.starter_unit_notional * self.config.starter_countertrend_ratio))
            return candidates
        if features.compression_regime and crushed_ask <= self.config.compression_crushed_price:
            candidates.insert(0, (crushed_side, crushed_ask, "late_compression_crushed_side", None))
        elif features.stabilization_regime and crushed_ask <= self.config.deep_crushed_price:
            candidates.insert(0, (crushed_side, crushed_ask, "post_expansion_stabilization_crushed_side", None))
        if features.expansion_regime and features.time_to_expiry_seconds > self.config.momentum_min_seconds_to_close:
            candidates.append((momentum_side, momentum_ask, "momentum_expansion_pyramid", None))
        return candidates

    def _dynamic_quantity(self, *, side: VolRegimeSide, price: float, features: VolRegimeFeatures, notional_override: float | None = None) -> float:
        if price <= 0 or price > self.config.max_price:
            return 0.0
        if notional_override is not None:
            notional = notional_override
        else:
            volatility_multiplier = 0.5 + min(3.0, features.atr_expansion_rate)
            distance_multiplier = 1.0 + min(2.0, abs(features.distance_from_strike) / 150.0)
            velocity_multiplier = 1.0 + min(2.0, features.velocity_away_from_strike / 8.0)
            if features.compression_regime:
                velocity_multiplier = max(1.0, velocity_multiplier * 0.75)
            notional = min(self.config.max_notional_per_add, self.config.base_notional * volatility_multiplier * distance_multiplier * velocity_multiplier)
        quantity = notional / max(price, self.config.floor_price)
        return round(quantity, 10)

    def _basis_ok(self, *, position: InventoryVolRegimePosition, side: VolRegimeSide, quantity: float, price: float, reason: str, features: VolRegimeFeatures) -> bool:
        if reason.startswith("starter_"):
            return True
        momentum_side: VolRegimeSide = "ABOVE" if features.distance_from_strike > 0 else "BELOW"
        if reason == "momentum_expansion_pyramid" and side == momentum_side:
            return True
        projected = position.projected_blended_basis(side=side, price=price, quantity=quantity)
        if projected is None:
            return True
        current = position.blended_basis
        if projected <= self.config.target_combined_basis:
            return True
        if current is not None and projected <= current - self.config.min_basis_improvement:
            return True
        return False

    def _risk_ok(self, *, position: InventoryVolRegimePosition, side: VolRegimeSide, quantity: float, price: float) -> bool:
        if position.total_cost + price * quantity > self.config.max_total_cost_per_market:
            return False
        if position.side_qty(side) + quantity > self.config.max_qty_per_side:
            return False
        return True

    def _cooldown_ok(self, event_key: str, ts: datetime) -> bool:
        last = self.last_fill_ts.get(event_key)
        return last is None or (ts - last).total_seconds() >= self.config.cooldown_seconds

    def _log(self, event_key: str, market_ticker: str, ts: datetime, side: str, reason: str, fill: InventoryVolRegimeFill | None, features: VolRegimeFeatures | None) -> None:
        self.events.append(
            {
                "event_key": event_key,
                "market_ticker": market_ticker,
                "ts": ts.isoformat(),
                "side": side,
                "event_type": "add" if fill else "scan",
                "price": fill.price if fill else None,
                "quantity": fill.quantity if fill else 0.0,
                "reason": reason,
                "raw_json": {"fill": fill.as_dict() if fill else None, "features": features.as_dict() if features else None},
            }
        )

    def research_metrics(self) -> dict[str, Any]:
        positions = list(self.positions.values())
        fills = [fill for position in positions for fill in position.fills]
        equity = [point for position in positions for point in position.equity_curve]
        feature_points = [point for position in positions for point in position.features_timeline]
        return {
            "inventory_additions_vs_atr_spikes": [fill.as_dict() for fill in fills],
            "additions_vs_velocity_away_from_strike": [
                {"ts": fill.ts.isoformat(), "side": fill.side, "quantity": fill.quantity, "velocity_away_from_strike": fill.features.get("velocity_away_from_strike")}
                for fill in fills
            ],
            "additions_vs_macd_histogram_extremes": [
                {"ts": fill.ts.isoformat(), "side": fill.side, "quantity": fill.quantity, "macd_histogram": fill.features.get("macd_histogram"), "macd_slope": fill.features.get("macd_slope")}
                for fill in fills
            ],
            "mark_to_market_equity_curve": equity,
            "blended_basis_over_time": [{"ts": point["ts"], "blended_basis": point.get("blended_basis")} for point in equity],
            "inventory_imbalance_heatmap": [
                {"ts": point["ts"], "ratio": point.get("inventory_imbalance_ratio"), "qty": point.get("inventory_imbalance_qty")}
                for point in equity
            ],
            "recovery_after_volatility_shocks": [point for point in equity if point.get("velocity_slowdown", 0.0) > 0.0],
            "compression_after_directional_expansion": [point for point in feature_points if point.get("compression_regime")],
            "volatility_regime_state": feature_points[-1] if feature_points else {},
            "atr_expansion_graph": [{"ts": point["ts"], "atr_1m": point["atr_1m"], "atr_expansion_rate": point["atr_expansion_rate"]} for point in feature_points],
            "distance_from_strike_velocity_graph": [{"ts": point["ts"], "distance_from_strike": point["distance_from_strike"], "velocity_away_from_strike": point["velocity_away_from_strike"]} for point in feature_points],
            "add_event_timeline": [fill.as_dict() for fill in fills],
            "per_side_inventory_ladder": [position.summary_dict() for position in positions],
        }
