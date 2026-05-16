from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean
from typing import Any, Literal

HedgeSide = Literal["UP", "DOWN"]


@dataclass(frozen=True)
class VolatilityHedgeConfig:
    unit_notional: float = 25.0
    trend_units: float = 3.0
    countertrend_units: float = 2.0
    add_notional: float = 50.0
    max_add_notional: float = 200.0
    floor_price: float = 0.05
    cheap_side_threshold: float = 0.35
    ideal_paired_cost: float = 0.98
    min_paired_cost_improvement: float = 0.005
    max_imbalance_ratio: float = 4.0
    stop_adding_seconds_to_expiry: float = 75.0
    starter_max_seconds_to_close: float = 14.0 * 60.0
    cooldown_seconds: float = 10.0
    slippage: float = 0.01
    fee_per_contract: float = 0.0
    atr_baseline: float = 20.0


@dataclass(frozen=True)
class VolatilityHedgeFeatures:
    time_to_expiry: float
    slope: float
    atr: float
    atr_expansion_rate: float
    distance_from_strike: float
    up_bid: float
    up_ask: float
    down_bid: float
    down_ask: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "time_to_expiry": self.time_to_expiry,
            "slope": self.slope,
            "atr": self.atr,
            "atr_expansion_rate": self.atr_expansion_rate,
            "distance_from_strike": self.distance_from_strike,
            "up_bid": self.up_bid,
            "up_ask": self.up_ask,
            "down_bid": self.down_bid,
            "down_ask": self.down_ask,
        }


@dataclass(frozen=True)
class VolatilityHedgeFill:
    event_key: str
    market_ticker: str
    side: HedgeSide
    price: float
    qty: float
    fee: float
    ts: datetime
    reason: str
    projected_paired_cost: float | None
    current_paired_cost: float | None
    features: dict[str, Any]

    @property
    def cost(self) -> float:
        return round(self.price * self.qty + self.fee, 10)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_key": self.event_key,
            "market_ticker": self.market_ticker,
            "side": self.side,
            "price": self.price,
            "qty": self.qty,
            "fee": self.fee,
            "cost": self.cost,
            "ts": self.ts.isoformat(),
            "reason": self.reason,
            "projected_paired_cost": self.projected_paired_cost,
            "current_paired_cost": self.current_paired_cost,
            "features": self.features,
        }


@dataclass(frozen=True)
class VolatilityHedgeDecision:
    fills: int
    reason: str
    fill_events: list[VolatilityHedgeFill] = field(default_factory=list)
    projected_paired_cost: float | None = None
    current_paired_cost: float | None = None
    features: VolatilityHedgeFeatures | None = None


@dataclass
class VolatilityHedgePosition:
    event_key: str
    market_ticker: str
    market_close_time: str
    strike: float
    up_qty: float = 0.0
    down_qty: float = 0.0
    up_cost: float = 0.0
    down_cost: float = 0.0
    last_ts: datetime | None = None
    last_features: dict[str, Any] = field(default_factory=dict)
    fills: list[VolatilityHedgeFill] = field(default_factory=list)

    @property
    def up_avg_entry(self) -> float:
        return round(self.up_cost / self.up_qty, 10) if self.up_qty else 0.0

    @property
    def down_avg_entry(self) -> float:
        return round(self.down_cost / self.down_qty, 10) if self.down_qty else 0.0

    @property
    def paired_qty(self) -> float:
        return min(self.up_qty, self.down_qty)

    @property
    def paired_cost(self) -> float | None:
        if not self.up_qty or not self.down_qty:
            return None
        return round(self.up_avg_entry + self.down_avg_entry, 10)

    @property
    def edge(self) -> float | None:
        if self.paired_cost is None:
            return None
        return round(1.0 - self.paired_cost, 10)

    @property
    def total_cost(self) -> float:
        return round(self.up_cost + self.down_cost, 10)

    @property
    def imbalance_ratio(self) -> float:
        small = min(self.up_qty, self.down_qty)
        large = max(self.up_qty, self.down_qty)
        if small <= 0:
            return float("inf") if large > 0 else 1.0
        return round(large / small, 10)

    @property
    def locked_payout(self) -> float:
        return self.paired_qty

    @property
    def locked_edge_dollars(self) -> float:
        return round((self.edge or 0.0) * self.paired_qty, 10)

    def side_qty(self, side: HedgeSide) -> float:
        return self.up_qty if side == "UP" else self.down_qty

    def projected_paired_cost(self, *, side: HedgeSide, price: float, qty: float) -> float | None:
        up_qty = self.up_qty + (qty if side == "UP" else 0.0)
        down_qty = self.down_qty + (qty if side == "DOWN" else 0.0)
        if up_qty <= 0 or down_qty <= 0:
            return None
        up_cost = self.up_cost + (price * qty if side == "UP" else 0.0)
        down_cost = self.down_cost + (price * qty if side == "DOWN" else 0.0)
        return round(up_cost / up_qty + down_cost / down_qty, 10)

    def projected_imbalance_ratio(self, *, side: HedgeSide, qty: float) -> float:
        up_qty = self.up_qty + (qty if side == "UP" else 0.0)
        down_qty = self.down_qty + (qty if side == "DOWN" else 0.0)
        small = min(up_qty, down_qty)
        large = max(up_qty, down_qty)
        if small <= 0:
            return float("inf") if large > 0 else 1.0
        return large / small

    def add_fill(self, *, side: HedgeSide, price: float, qty: float, fee: float, ts: datetime, reason: str, projected_paired_cost: float | None, features: VolatilityHedgeFeatures) -> VolatilityHedgeFill:
        fill = VolatilityHedgeFill(
            event_key=self.event_key,
            market_ticker=self.market_ticker,
            side=side,
            price=price,
            qty=qty,
            fee=fee,
            ts=ts,
            reason=reason,
            projected_paired_cost=projected_paired_cost,
            current_paired_cost=self.paired_cost,
            features=features.as_dict(),
        )
        if side == "UP":
            self.up_qty += qty
            self.up_cost += fill.cost
        else:
            self.down_qty += qty
            self.down_cost += fill.cost
        self.last_ts = ts
        self.last_features = features.as_dict()
        self.fills.append(fill)
        return fill

    def summary_dict(self) -> dict[str, Any]:
        return {
            "event_key": self.event_key,
            "market_ticker": self.market_ticker,
            "market_close_time": self.market_close_time,
            "strike": self.strike,
            "up_qty": self.up_qty,
            "down_qty": self.down_qty,
            "up_avg_entry": self.up_avg_entry,
            "down_avg_entry": self.down_avg_entry,
            "paired_qty": self.paired_qty,
            "paired_cost": self.paired_cost,
            "edge": self.edge,
            "locked_payout": self.locked_payout,
            "locked_edge_dollars": self.locked_edge_dollars,
            "imbalance_ratio": self.imbalance_ratio,
            "total_cost": self.total_cost,
            "last_ts": self.last_ts.isoformat() if self.last_ts else None,
            "last_features_json": self.last_features,
            "fills_json": [fill.as_dict() for fill in self.fills],
        }


class VolatilityHedgeManager:
    def __init__(self, config: VolatilityHedgeConfig | None = None) -> None:
        self.config = config or VolatilityHedgeConfig()
        self.positions: dict[str, VolatilityHedgePosition] = {}
        self.history: dict[str, list[dict[str, Any]]] = {}
        self.events: list[dict[str, Any]] = []
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
        slope: float | None,
        up_bid: float | None,
        up_ask: float | None,
        down_bid: float | None,
        down_ask: float | None,
        atr: float | None = None,
    ) -> VolatilityHedgeDecision:
        if not self._valid_book(up_bid, up_ask, down_bid, down_ask):
            return self._decision(event_key, market_ticker, ts, "invalid_book", None, None, None, None)
        assert up_bid is not None and up_ask is not None and down_bid is not None and down_ask is not None
        features = self._features(event_key, ts, btc_price, seconds_to_close, distance_from_strike, slope, atr, up_bid, up_ask, down_bid, down_ask)
        position = self.positions.setdefault(
            event_key,
            VolatilityHedgePosition(event_key=event_key, market_ticker=market_ticker, market_close_time=market_close_time, strike=strike),
        )
        if position.total_cost == 0:
            return self._open_initial(position, ts, features)
        if seconds_to_close <= self.config.stop_adding_seconds_to_expiry:
            return self._decision(event_key, market_ticker, ts, "stop_near_expiry", position, None, None, features)
        if not self._cooldown_ok(event_key, ts):
            return self._decision(event_key, market_ticker, ts, "cooldown", position, None, None, features)
        side, ask = self._preferred_add_side(features)
        price = round(ask + self.config.slippage, 10)
        qty = self._add_qty(price=price, features=features)
        projected = position.projected_paired_cost(side=side, price=price, qty=qty)
        current = position.paired_cost
        if position.projected_imbalance_ratio(side=side, qty=qty) > self.config.max_imbalance_ratio:
            return self._decision(event_key, market_ticker, ts, "max_imbalance_ratio", position, projected, side, features)
        if not self._improves_hedge(current=current, projected=projected):
            return self._decision(event_key, market_ticker, ts, "projected_paired_cost_not_improved", position, projected, side, features)
        fill = position.add_fill(side=side, price=price, qty=qty, fee=qty * self.config.fee_per_contract, ts=ts, reason="volatility_cheap_side_add", projected_paired_cost=projected, features=features)
        self.last_fill_ts[event_key] = ts
        self._log(position.event_key, position.market_ticker, ts, side, "volatility_cheap_side_add", position, projected, features, fill=fill)
        return VolatilityHedgeDecision(1, "volatility_cheap_side_add", [fill], projected, current, features)

    def _open_initial(self, position: VolatilityHedgePosition, ts: datetime, features: VolatilityHedgeFeatures) -> VolatilityHedgeDecision:
        trend_side: HedgeSide = "UP" if features.slope >= 0 else "DOWN"
        counter_side: HedgeSide = "DOWN" if trend_side == "UP" else "UP"
        asks = {"UP": features.up_ask, "DOWN": features.down_ask}
        fills: list[VolatilityHedgeFill] = []
        starter_legs: tuple[tuple[HedgeSide, float, str], ...] = (
            (trend_side, self.config.trend_units, "initial_trend_3_units"),
            (counter_side, self.config.countertrend_units, "initial_countertrend_2_units"),
        )
        for side, units, reason in starter_legs:
            price = round(asks[side] + self.config.slippage, 10)
            qty = round((self.config.unit_notional * units) / max(price, self.config.floor_price), 10)
            projected = position.projected_paired_cost(side=side, price=price, qty=qty)
            fill = position.add_fill(side=side, price=price, qty=qty, fee=qty * self.config.fee_per_contract, ts=ts, reason=reason, projected_paired_cost=projected, features=features)
            fills.append(fill)
            self._log(position.event_key, position.market_ticker, ts, side, reason, position, projected, features, fill=fill)
        self.last_fill_ts[position.event_key] = ts
        return VolatilityHedgeDecision(len(fills), "initial_3_to_2_hedge", fills, position.paired_cost, None, features)

    def _features(self, event_key: str, ts: datetime, btc_price: float, seconds_to_close: float, distance_from_strike: float, slope: float | None, atr: float | None, up_bid: float, up_ask: float, down_bid: float, down_ask: float) -> VolatilityHedgeFeatures:
        history = self.history.setdefault(event_key, [])
        if atr is None:
            recent = [abs(item["btc_price"] - prev["btc_price"]) for item, prev in zip(history[-59:], history[-60:-1], strict=False)]
            atr = mean(recent) if recent else 0.0
        baseline = mean([item["atr"] for item in history[-180:] if item.get("atr") is not None] or [self.config.atr_baseline])
        features = VolatilityHedgeFeatures(
            time_to_expiry=round(seconds_to_close, 10),
            slope=round(float(slope or 0.0), 10),
            atr=round(float(atr), 10),
            atr_expansion_rate=round(float(atr) / max(baseline, 1e-9), 10),
            distance_from_strike=round(distance_from_strike, 10),
            up_bid=up_bid,
            up_ask=up_ask,
            down_bid=down_bid,
            down_ask=down_ask,
        )
        history.append({"ts": ts.isoformat(), "btc_price": btc_price, **features.as_dict()})
        if len(history) > 600:
            del history[:-600]
        return features

    def _preferred_add_side(self, features: VolatilityHedgeFeatures) -> tuple[HedgeSide, float]:
        # Prefer the side made cheap by movement away from it; otherwise choose lowest ask.
        if features.distance_from_strike > 0 and features.down_ask <= self.config.cheap_side_threshold:
            return "DOWN", features.down_ask
        if features.distance_from_strike < 0 and features.up_ask <= self.config.cheap_side_threshold:
            return "UP", features.up_ask
        return ("UP", features.up_ask) if features.up_ask <= features.down_ask else ("DOWN", features.down_ask)

    def _add_qty(self, *, price: float, features: VolatilityHedgeFeatures) -> float:
        vol_mult = 0.5 + min(3.0, features.atr_expansion_rate)
        distance_mult = 1.0 + min(2.0, abs(features.distance_from_strike) / 150.0)
        notional = min(self.config.max_add_notional, self.config.add_notional * vol_mult * distance_mult)
        return round(notional / max(price, self.config.floor_price), 10)

    def _improves_hedge(self, *, current: float | None, projected: float | None) -> bool:
        if projected is None:
            return False
        if current is not None and projected <= current - self.config.min_paired_cost_improvement:
            return True
        if current is not None and current > self.config.ideal_paired_cost and projected <= self.config.ideal_paired_cost:
            return True
        return False

    def _valid_book(self, up_bid: float | None, up_ask: float | None, down_bid: float | None, down_ask: float | None) -> bool:
        values = [up_bid, up_ask, down_bid, down_ask]
        if any(v is None for v in values):
            return False
        assert up_bid is not None and up_ask is not None and down_bid is not None and down_ask is not None
        if min(up_bid, up_ask, down_bid, down_ask) <= 0 or max(up_bid, up_ask, down_bid, down_ask) > 1:
            return False
        return up_bid <= up_ask and down_bid <= down_ask

    def _cooldown_ok(self, event_key: str, ts: datetime) -> bool:
        last = self.last_fill_ts.get(event_key)
        return last is None or (ts - last).total_seconds() >= self.config.cooldown_seconds

    def _decision(self, event_key: str, market_ticker: str, ts: datetime, reason: str, position: VolatilityHedgePosition | None, projected: float | None, side: HedgeSide | None, features: VolatilityHedgeFeatures | None) -> VolatilityHedgeDecision:
        self._log(event_key, market_ticker, ts, side or "NONE", reason, position, projected, features)
        return VolatilityHedgeDecision(0, reason, [], projected, position.paired_cost if position else None, features)

    def _log(self, event_key: str, market_ticker: str, ts: datetime, side: str, reason: str, position: VolatilityHedgePosition | None, projected: float | None, features: VolatilityHedgeFeatures | None, fill: VolatilityHedgeFill | None = None) -> None:
        feature_dict = features.as_dict() if features else {}
        self.events.append(
            {
                "event_key": event_key,
                "market_ticker": market_ticker,
                "ts": ts.isoformat(),
                "side": side,
                "event_type": "fill" if fill else "decision",
                "reason": reason,
                "price": fill.price if fill else None,
                "qty": fill.qty if fill else 0.0,
                "projected_paired_cost": projected,
                "current_paired_cost": position.paired_cost if position else None,
                "edge": position.edge if position else None,
                "up_qty": position.up_qty if position else 0.0,
                "down_qty": position.down_qty if position else 0.0,
                "time_to_expiry": feature_dict.get("time_to_expiry"),
                "slope": feature_dict.get("slope"),
                "atr": feature_dict.get("atr"),
                "distance_from_strike": feature_dict.get("distance_from_strike"),
                "up_bid": feature_dict.get("up_bid"),
                "up_ask": feature_dict.get("up_ask"),
                "down_bid": feature_dict.get("down_bid"),
                "down_ask": feature_dict.get("down_ask"),
                "raw_json": {"fill": fill.as_dict() if fill else None, "features": feature_dict},
            }
        )


def position_from_summary(row: dict[str, Any]) -> VolatilityHedgePosition:
    up_qty = float(row.get("up_qty") or 0.0)
    down_qty = float(row.get("down_qty") or 0.0)
    position = VolatilityHedgePosition(
        event_key=str(row["event_key"]),
        market_ticker=str(row["market_ticker"]),
        market_close_time=str(row["market_close_time"]),
        strike=float(row["strike"]),
        up_qty=up_qty,
        down_qty=down_qty,
        up_cost=float(row.get("up_avg_entry") or 0.0) * up_qty,
        down_cost=float(row.get("down_avg_entry") or 0.0) * down_qty,
    )
    last_ts = row.get("updated_at") or row.get("last_ts")
    if last_ts:
        try:
            position.last_ts = datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
        except ValueError:
            position.last_ts = None
    features = row.get("last_features_json")
    if isinstance(features, dict):
        position.last_features = features
    elif features:
        try:
            loaded = json.loads(str(features))
            position.last_features = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            position.last_features = {}
    return position
