from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from math import erf, inf, sqrt
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
    min_projected_pair_cost_improvement: float | None = None
    max_imbalance_ratio: float = 4.0
    target_lean_ratio: float = 1.5
    soft_imbalance_ratio: float = 1.5
    repair_imbalance_ratio: float = 2.0
    hard_imbalance_ratio: float = 3.0
    emergency_imbalance_ratio: float = 4.0
    max_notional_per_market: float = 250.0
    max_notional_per_side: float = 150.0
    max_contracts_per_add: float = 50.0
    base_add_notional: float = 10.0
    repair_add_notional: float = 10.0
    require_seed_pair_cost_below: float = 1.04
    max_initial_ask_sum: float = 1.03
    max_worst_case_loss_per_market: float | None = None
    max_settlement_ev_worsening: float = 0.50
    allow_seed_loss: bool = True
    total_contract_seconds: float = 900.0
    observe_seconds: float = 30.0
    early_seed_until_seconds: float = 180.0
    main_harvest_until_seconds: float = 600.0
    repair_protect_until_seconds: float = 810.0
    stop_new_seed_seconds_before_expiry: float = 180.0
    stop_normal_add_seconds_before_expiry: float = 90.0
    stop_adding_seconds_to_expiry: float = 75.0
    starter_max_seconds_to_close: float = 14.0 * 60.0
    cooldown_seconds: float = 10.0
    slippage: float = 0.01
    fee_per_contract: float = 0.0
    atr_baseline: float = 20.0

    @property
    def pair_cost_improvement_threshold(self) -> float:
        return self.min_projected_pair_cost_improvement if self.min_projected_pair_cost_improvement is not None else self.min_paired_cost_improvement


class LifecyclePhase(StrEnum):
    OBSERVE = "OBSERVE"
    EARLY_SEED = "EARLY_SEED"
    MAIN_HARVEST = "MAIN_HARVEST"
    REPAIR_PROTECT = "REPAIR_PROTECT"
    LATE_SETTLEMENT = "LATE_SETTLEMENT"


@dataclass(frozen=True)
class LifecycleState:
    phase: LifecyclePhase
    elapsed_seconds: float
    time_to_expiry: float
    phase_seed_pair_threshold: float
    phase_max_initial_ask_sum: float
    phase_allow_seed_loss: bool
    seed_window_open: bool
    normal_add_window_open: bool
    repair_only: bool

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "lifecycle_phase": self.phase,
            "elapsed_seconds": self.elapsed_seconds,
            "time_to_expiry": self.time_to_expiry,
            "phase_seed_pair_threshold": self.phase_seed_pair_threshold,
            "phase_max_initial_ask_sum": self.phase_max_initial_ask_sum,
            "seed_window_open": self.seed_window_open,
            "normal_add_window_open": self.normal_add_window_open,
            "repair_only": self.repair_only,
        }


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

    def summary_dict(self, config: VolatilityHedgeConfig | None = None) -> dict[str, Any]:
        cfg = config or VolatilityHedgeConfig()
        quality = PositionBalancer(cfg).metrics(self, _features_from_position(self))
        lifecycle = lifecycle_for_time(float((self.last_features or {}).get("time_to_expiry") or 0.0), cfg)
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
            "mode": quality.mode,
            "target_ratio": quality.target_ratio,
            "larger_side": quality.larger_side,
            "smaller_side": quality.smaller_side,
            "repair_qty_needed": quality.repair_qty_needed,
            "p_up": quality.p_up,
            "p_down": quality.p_down,
            "expected_settlement_value": quality.expected_settlement_value,
            "settlement_EV": quality.settlement_ev,
            "EV_per_dollar": quality.ev_per_dollar,
            "up_win_pnl": quality.up_win_pnl,
            "down_win_pnl": quality.down_win_pnl,
            "worst_case_pnl": quality.worst_case_pnl,
            "best_case_pnl": quality.best_case_pnl,
            "locked_edge": quality.locked_edge,
            "residual_up_qty": quality.residual_up_qty,
            "residual_down_qty": quality.residual_down_qty,
            "residual_EV": quality.residual_ev,
            "max_market_notional": quality.max_market_notional,
            "notional_used": quality.notional_used,
            "notional_remaining": quality.notional_remaining,
            "lifecycle_phase": lifecycle.phase,
            "elapsed_seconds": lifecycle.elapsed_seconds,
            "last_ts": self.last_ts.isoformat() if self.last_ts else None,
            "last_features_json": self.last_features,
            "fills_json": [fill.as_dict() for fill in self.fills],
        }


class PositionMode(StrEnum):
    SEED = "SEED"
    NORMAL = "NORMAL"
    REPAIR = "REPAIR"
    LOCKED = "LOCKED"
    EMERGENCY = "EMERGENCY"


@dataclass(frozen=True)
class HedgeProposal:
    side: HedgeSide
    price: float
    qty: float
    reason: str
    confidence: float
    features: VolatilityHedgeFeatures


@dataclass(frozen=True)
class PositionQualityMetrics:
    mode: PositionMode
    target_ratio: float
    imbalance_ratio: float
    larger_side: HedgeSide | None
    smaller_side: HedgeSide | None
    repair_qty_needed: float
    p_up: float
    p_down: float
    total_cost: float
    expected_settlement_value: float
    settlement_ev: float
    ev_per_dollar: float | None
    up_win_pnl: float
    down_win_pnl: float
    worst_case_pnl: float
    best_case_pnl: float
    paired_qty: float
    paired_cost: float | None
    locked_edge: float
    residual_up_qty: float
    residual_down_qty: float
    residual_ev: float
    max_market_notional: float
    notional_used: float
    notional_remaining: float


@dataclass(frozen=True)
class BalanceDecision:
    allowed: bool
    reason: str
    mode: PositionMode
    proposed_qty: float
    final_qty: float
    price: float
    side: HedgeSide
    current_imbalance_ratio: float
    projected_imbalance_ratio: float
    current_paired_cost: float | None
    projected_paired_cost: float | None
    current_settlement_ev: float
    projected_settlement_ev: float
    current_worst_case_pnl: float
    projected_worst_case_pnl: float
    repair_qty_needed: float
    current_metrics: PositionQualityMetrics
    projected_metrics: PositionQualityMetrics


class PositionBalancer:
    def __init__(self, config: VolatilityHedgeConfig | None = None) -> None:
        self.config = config or VolatilityHedgeConfig()

    def metrics(self, position: VolatilityHedgePosition, features: VolatilityHedgeFeatures) -> PositionQualityMetrics:
        total_cost = position.total_cost
        p_up = self._p_up(features)
        p_down = 1.0 - p_up
        expected = position.up_qty * p_up + position.down_qty * p_down
        settlement_ev = expected - total_cost
        up_win = position.up_qty - total_cost
        down_win = position.down_qty - total_cost
        worst = min(up_win, down_win)
        best = max(up_win, down_win)
        larger_side, smaller_side = self._sides(position.up_qty, position.down_qty)
        repair_needed = self.repair_qty_needed(position)
        locked_edge = position.locked_edge_dollars
        residual_up = max(0.0, position.up_qty - position.down_qty)
        residual_down = max(0.0, position.down_qty - position.up_qty)
        return PositionQualityMetrics(
            mode=self.mode_for(position),
            target_ratio=self.config.target_lean_ratio,
            imbalance_ratio=position.imbalance_ratio,
            larger_side=larger_side,
            smaller_side=smaller_side,
            repair_qty_needed=repair_needed,
            p_up=round(p_up, 10),
            p_down=round(p_down, 10),
            total_cost=total_cost,
            expected_settlement_value=round(expected, 10),
            settlement_ev=round(settlement_ev, 10),
            ev_per_dollar=round(settlement_ev / total_cost, 10) if total_cost > 0 else None,
            up_win_pnl=round(up_win, 10),
            down_win_pnl=round(down_win, 10),
            worst_case_pnl=round(worst, 10),
            best_case_pnl=round(best, 10),
            paired_qty=position.paired_qty,
            paired_cost=position.paired_cost,
            locked_edge=locked_edge,
            residual_up_qty=round(residual_up, 10),
            residual_down_qty=round(residual_down, 10),
            residual_ev=round(settlement_ev - locked_edge, 10),
            max_market_notional=self.config.max_notional_per_market,
            notional_used=total_cost,
            notional_remaining=round(self.config.max_notional_per_market - total_cost, 10),
        )

    def evaluate(self, position: VolatilityHedgePosition, proposal: HedgeProposal) -> BalanceDecision:
        current = self.metrics(position, proposal.features)
        mode = current.mode
        larger = current.larger_side
        smaller = current.smaller_side
        final_qty = min(proposal.qty, self.config.max_contracts_per_add)
        if mode in {PositionMode.REPAIR, PositionMode.EMERGENCY} and smaller == proposal.side:
            affordable = self.config.repair_add_notional / max(proposal.price, self.config.floor_price)
            final_qty = min(final_qty, max(0.0, current.repair_qty_needed), affordable)
        else:
            affordable = self.config.base_add_notional / max(proposal.price, self.config.floor_price)
            final_qty = min(final_qty, affordable)
        if final_qty <= 0:
            return self._decision(False, "max_contracts_per_add", mode, position, proposal, 0.0, current)
        projected = self._project(position, proposal.side, proposal.price, final_qty, proposal.features)
        if mode == PositionMode.NORMAL and current.imbalance_ratio <= self.config.repair_imbalance_ratio and projected.imbalance_ratio > self.config.repair_imbalance_ratio:
            final_qty = self._cap_qty_for_imbalance(position, proposal, final_qty, self.config.repair_imbalance_ratio, current)
            if final_qty <= 0:
                return self._decision(False, "max_imbalance_ratio", mode, position, proposal, 0.0, current, projected)
            projected = self._project(position, proposal.side, proposal.price, final_qty, proposal.features)
        if mode in {PositionMode.REPAIR, PositionMode.EMERGENCY} and proposal.side == larger:
            return self._decision(False, "repair_mode_blocks_larger_side", mode, position, proposal, final_qty, current, projected)
        if current.imbalance_ratio > self.config.soft_imbalance_ratio and proposal.side == larger and projected.imbalance_ratio >= current.imbalance_ratio:
            return self._decision(False, "projected_imbalance_worse", mode, position, proposal, final_qty, current, projected)
        if projected.total_cost > self.config.max_notional_per_market + 1e-9:
            return self._decision(False, "max_market_notional", mode, position, proposal, final_qty, current, projected)
        if self._side_notional(position, proposal.side, proposal.price, final_qty) > self.config.max_notional_per_side + 1e-9:
            return self._decision(False, "max_side_notional", mode, position, proposal, final_qty, current, projected)
        if projected.imbalance_ratio > self.config.emergency_imbalance_ratio and proposal.side != smaller:
            return self._decision(False, "max_imbalance_ratio", mode, position, proposal, final_qty, current, projected)
        if self.config.max_worst_case_loss_per_market is not None and projected.worst_case_pnl < -abs(self.config.max_worst_case_loss_per_market):
            return self._decision(False, "worst_case_pnl_limit", mode, position, proposal, final_qty, current, projected)
        pair_improved = _cost_improved(current.paired_cost, projected.paired_cost, self.config.pair_cost_improvement_threshold)
        imbalance_improved = projected.imbalance_ratio < current.imbalance_ratio
        ev_improved = projected.settlement_ev >= current.settlement_ev - self.config.max_settlement_ev_worsening
        if mode == PositionMode.NORMAL:
            if not pair_improved:
                return self._decision(False, "projected_paired_cost_not_improved", mode, position, proposal, final_qty, current, projected)
            if not ev_improved:
                return self._decision(False, "projected_settlement_ev_worse", mode, position, proposal, final_qty, current, projected)
            if projected.imbalance_ratio > self.config.repair_imbalance_ratio + 1e-8:
                return self._decision(False, "max_imbalance_ratio", mode, position, proposal, final_qty, current, projected)
        elif mode == PositionMode.REPAIR:
            if proposal.side != smaller:
                return self._decision(False, "repair_mode_blocks_larger_side", mode, position, proposal, final_qty, current, projected)
            if not imbalance_improved:
                return self._decision(False, "projected_imbalance_worse", mode, position, proposal, final_qty, current, projected)
            if not ev_improved and not pair_improved:
                return self._decision(False, "projected_settlement_ev_worse", mode, position, proposal, final_qty, current, projected)
        elif mode == PositionMode.EMERGENCY:
            if proposal.side != smaller:
                return self._decision(False, "repair_mode_blocks_larger_side", mode, position, proposal, final_qty, current, projected)
            if not imbalance_improved:
                return self._decision(False, "projected_imbalance_worse", mode, position, proposal, final_qty, current, projected)
        elif mode == PositionMode.LOCKED and not (pair_improved and projected.paired_cost is not None and projected.paired_cost <= self.config.ideal_paired_cost - 0.02):
            return self._decision(False, "locked_position_no_strong_edge", mode, position, proposal, final_qty, current, projected)
        return self._decision(True, proposal.reason, mode, position, proposal, final_qty, current, projected)

    def mode_for(self, position: VolatilityHedgePosition) -> PositionMode:
        if position.total_cost <= 0:
            return PositionMode.SEED
        ratio = position.imbalance_ratio
        if ratio > self.config.emergency_imbalance_ratio:
            return PositionMode.EMERGENCY
        if ratio > self.config.repair_imbalance_ratio:
            return PositionMode.REPAIR
        if ratio < self.config.soft_imbalance_ratio and position.paired_cost is not None and position.paired_cost <= self.config.ideal_paired_cost:
            return PositionMode.LOCKED
        return PositionMode.NORMAL

    def repair_qty_needed(self, position: VolatilityHedgePosition) -> float:
        larger = max(position.up_qty, position.down_qty)
        smaller = min(position.up_qty, position.down_qty)
        if larger <= 0:
            return 0.0
        return max(0.0, round(larger / self.config.target_lean_ratio - smaller, 10))

    def _project(self, position: VolatilityHedgePosition, side: HedgeSide, price: float, qty: float, features: VolatilityHedgeFeatures) -> PositionQualityMetrics:
        projected = VolatilityHedgePosition(position.event_key, position.market_ticker, position.market_close_time, position.strike)
        projected.up_qty = position.up_qty
        projected.down_qty = position.down_qty
        projected.up_cost = position.up_cost
        projected.down_cost = position.down_cost
        projected.add_fill(side=side, price=price, qty=qty, fee=qty * self.config.fee_per_contract, ts=datetime.fromisoformat("2026-01-01T00:00:00+00:00"), reason="projection", projected_paired_cost=None, features=features)
        return self.metrics(projected, features)

    def _cap_qty_for_imbalance(self, position: VolatilityHedgePosition, proposal: HedgeProposal, qty: float, max_ratio: float, current: PositionQualityMetrics) -> float:
        if qty <= 0 or current.imbalance_ratio > max_ratio:
            return 0.0
        low = 0.0
        high = qty
        best = 0.0
        for _ in range(40):
            mid = (low + high) / 2.0
            projected = self._project(position, proposal.side, proposal.price, mid, proposal.features)
            if projected.imbalance_ratio <= max_ratio + 1e-9:
                best = mid
                low = mid
            else:
                high = mid
        return round(best, 10)

    def _decision(self, allowed: bool, reason: str, mode: PositionMode, position: VolatilityHedgePosition, proposal: HedgeProposal, final_qty: float, current: PositionQualityMetrics, projected: PositionQualityMetrics | None = None) -> BalanceDecision:
        projected = projected or current
        return BalanceDecision(
            allowed=allowed,
            reason=reason,
            mode=mode,
            proposed_qty=proposal.qty,
            final_qty=final_qty if allowed else 0.0,
            price=proposal.price,
            side=proposal.side,
            current_imbalance_ratio=current.imbalance_ratio,
            projected_imbalance_ratio=projected.imbalance_ratio,
            current_paired_cost=current.paired_cost,
            projected_paired_cost=projected.paired_cost,
            current_settlement_ev=current.settlement_ev,
            projected_settlement_ev=projected.settlement_ev,
            current_worst_case_pnl=current.worst_case_pnl,
            projected_worst_case_pnl=projected.worst_case_pnl,
            repair_qty_needed=current.repair_qty_needed,
            current_metrics=current,
            projected_metrics=projected,
        )

    def _p_up(self, features: VolatilityHedgeFeatures) -> float:
        if features.up_bid > 0 and features.up_ask > 0 and features.up_bid <= features.up_ask <= 1:
            return min(0.99, max(0.01, (features.up_bid + features.up_ask) / 2.0))
        denom = max(features.atr * sqrt(max(features.time_to_expiry, 1.0) / 60.0), 1e-9)
        z = features.distance_from_strike / denom
        return min(0.99, max(0.01, 0.5 * (1.0 + erf(z / sqrt(2.0)))))

    def _sides(self, up_qty: float, down_qty: float) -> tuple[HedgeSide | None, HedgeSide | None]:
        if up_qty == down_qty:
            return None, None
        return ("UP", "DOWN") if up_qty > down_qty else ("DOWN", "UP")

    def _side_notional(self, position: VolatilityHedgePosition, side: HedgeSide, price: float, qty: float) -> float:
        current = position.up_cost if side == "UP" else position.down_cost
        return current + price * qty


class VolatilityHedgeManager:
    def __init__(self, config: VolatilityHedgeConfig | None = None) -> None:
        self.config = config or VolatilityHedgeConfig()
        self.positions: dict[str, VolatilityHedgePosition] = {}
        self.history: dict[str, list[dict[str, Any]]] = {}
        self.events: list[dict[str, Any]] = []
        self.last_fill_ts: dict[str, datetime] = {}
        self.balancer = PositionBalancer(self.config)

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
        lifecycle = lifecycle_for_time(seconds_to_close, self.config)
        if not self._valid_book(up_bid, up_ask, down_bid, down_ask):
            return self._decision(event_key, market_ticker, ts, "invalid_book", None, None, None, None, lifecycle=lifecycle)
        assert up_bid is not None and up_ask is not None and down_bid is not None and down_ask is not None
        try:
            canonical_event_key = event_key_for_volatility_hedge(market_ticker, market_close_time, strike)
        except ValueError:
            features = self._features(event_key, ts, btc_price, seconds_to_close, distance_from_strike, slope, atr, up_bid, up_ask, down_bid, down_ask)
            return self._decision(event_key, market_ticker, ts, "invalid_event_key", None, None, None, features, lifecycle=lifecycle)
        features = self._features(canonical_event_key, ts, btc_price, seconds_to_close, distance_from_strike, slope, atr, up_bid, up_ask, down_bid, down_ask)
        position = self.positions.get(canonical_event_key) or self.positions.get(event_key)
        if seconds_to_close <= 0:
            return self._decision(canonical_event_key, market_ticker, ts, "expired_no_add", position, None, None, features, lifecycle=lifecycle)
        if position is None:
            return self._open_initial(canonical_event_key, market_ticker, market_close_time, strike, ts, features, lifecycle, alias_key=event_key if event_key != canonical_event_key else None)
        cooldown_key = position.event_key
        if not self._cooldown_ok(cooldown_key, ts):
            return self._decision(position.event_key, position.market_ticker, ts, "cooldown", position, None, None, features, lifecycle=lifecycle)
        side, ask = self._preferred_add_side(features)
        price = round(ask + self.config.slippage, 10)
        qty = self._add_qty(price=price, features=features)
        proposal = HedgeProposal(side=side, price=price, qty=qty, reason="volatility_cheap_side_add", confidence=0.75, features=features)
        balance = self.balancer.evaluate(position, proposal)
        if lifecycle.repair_only and not self._lifecycle_allows_repair(balance, lifecycle):
            return self._decision(event_key, market_ticker, ts, "normal_add_window_closed", position, balance.projected_paired_cost, side, features, balance=balance, lifecycle=lifecycle)
        if not balance.allowed:
            return self._decision(event_key, market_ticker, ts, balance.reason, position, balance.projected_paired_cost, side, features, balance=balance, lifecycle=lifecycle)
        fill = position.add_fill(side=side, price=price, qty=balance.final_qty, fee=balance.final_qty * self.config.fee_per_contract, ts=ts, reason="volatility_cheap_side_add", projected_paired_cost=balance.projected_paired_cost, features=features)
        self.last_fill_ts[position.event_key] = ts
        if event_key != position.event_key:
            self.last_fill_ts[event_key] = ts
        self._log(position.event_key, position.market_ticker, ts, side, "volatility_cheap_side_add", position, balance.projected_paired_cost, features, fill=fill, balance=balance, lifecycle=lifecycle)
        return VolatilityHedgeDecision(1, "volatility_cheap_side_add", [fill], balance.projected_paired_cost, balance.current_paired_cost, features)

    def _open_initial(self, event_key: str, market_ticker: str, market_close_time: str, strike: float, ts: datetime, features: VolatilityHedgeFeatures, lifecycle: LifecycleState, alias_key: str | None = None) -> VolatilityHedgeDecision:
        trend_side: HedgeSide = "UP" if features.slope >= 0 else "DOWN"
        counter_side: HedgeSide = "DOWN" if trend_side == "UP" else "UP"
        asks: dict[HedgeSide, float] = {"UP": features.up_ask, "DOWN": features.down_ask}
        simple_ask_sum = round(asks["UP"] + asks["DOWN"], 10)
        projected_pair_cost = round(simple_ask_sum + (2 * self.config.slippage), 10)
        seed_reject = self._seed_rejection_reason(lifecycle, simple_ask_sum, projected_pair_cost)
        if seed_reject is not None:
            reason, failed_reason = seed_reject
            self._log(event_key, market_ticker, ts, "NONE", reason, None, projected_pair_cost, features, lifecycle=lifecycle, seed_gate_failed_reason=failed_reason)
            return VolatilityHedgeDecision(0, reason, [], projected_pair_cost, None, features)
        position = VolatilityHedgePosition(event_key=event_key, market_ticker=market_ticker, market_close_time=market_close_time, strike=strike)
        fills: list[VolatilityHedgeFill] = []
        prices = {side: round(ask + self.config.slippage, 10) for side, ask in asks.items()}
        starter_legs: tuple[tuple[HedgeSide, float, str], ...] = (
            (trend_side, self.config.trend_units, "initial_trend_3_units"),
            (counter_side, self.config.countertrend_units, "initial_countertrend_2_units"),
        )
        unit_qty = self._seed_unit_qty(starter_legs, prices)
        seed_reject = self._seed_risk_rejection(position, starter_legs, prices, unit_qty, features)
        if seed_reject is not None:
            self._log(event_key, market_ticker, ts, "NONE", seed_reject, None, projected_pair_cost, features, lifecycle=lifecycle, seed_gate_failed_reason=seed_reject)
            return VolatilityHedgeDecision(0, seed_reject, [], projected_pair_cost, None, features)
        for side, units, reason in starter_legs:
            price = prices[side]
            qty = round(unit_qty * units, 10)
            projected = position.projected_paired_cost(side=side, price=price, qty=qty)
            fill = position.add_fill(side=side, price=price, qty=qty, fee=qty * self.config.fee_per_contract, ts=ts, reason=reason, projected_paired_cost=projected, features=features)
            fills.append(fill)
            self._log(position.event_key, position.market_ticker, ts, side, reason, position, projected, features, fill=fill, lifecycle=lifecycle)
        self.last_fill_ts[position.event_key] = ts
        self.positions[position.event_key] = position
        if alias_key:
            self.last_fill_ts[alias_key] = ts
            self.positions[alias_key] = position
        return VolatilityHedgeDecision(len(fills), "initial_3_to_2_hedge", fills, position.paired_cost, None, features)

    def _seed_rejection_reason(self, lifecycle: LifecycleState, simple_ask_sum: float, projected_pair_cost: float) -> tuple[str, str] | None:
        if lifecycle.phase == LifecyclePhase.LATE_SETTLEMENT:
            return "late_settlement_no_new_seed", "seed_window_closed"
        if lifecycle.phase == LifecyclePhase.REPAIR_PROTECT:
            return "repair_protect_no_new_seed", "seed_window_closed"
        if not lifecycle.seed_window_open:
            return "seed_window_closed", "seed_window_closed"
        phase_reason = {
            LifecyclePhase.OBSERVE: "observe_phase_seed_too_expensive",
            LifecyclePhase.EARLY_SEED: "early_seed_pair_cost_too_high",
            LifecyclePhase.MAIN_HARVEST: "main_seed_pair_cost_too_high",
        }.get(lifecycle.phase, "seed_pair_cost_too_high")
        if simple_ask_sum > lifecycle.phase_max_initial_ask_sum + 1e-9:
            return phase_reason, "simple_ask_sum_above_phase_max_initial_ask_sum"
        if projected_pair_cost > lifecycle.phase_seed_pair_threshold + 1e-9:
            return phase_reason, "projected_seed_pair_cost_above_phase_threshold"
        if not lifecycle.phase_allow_seed_loss and projected_pair_cost > 1.0 + 1e-9:
            return phase_reason, "seed_loss_not_allowed"
        return None

    def _seed_unit_qty(self, starter_legs: tuple[tuple[HedgeSide, float, str], ...], prices: dict[HedgeSide, float]) -> float:
        weighted_price = sum(units * max(prices[side], self.config.floor_price) for side, units, _ in starter_legs)
        if weighted_price <= 0:
            return 0.0
        unit_qty = self.config.base_add_notional / weighted_price
        total_qty = sum(units * unit_qty for _, units, _ in starter_legs)
        if total_qty > self.config.max_contracts_per_add:
            unit_qty *= self.config.max_contracts_per_add / total_qty
        return round(unit_qty, 10)

    def _seed_risk_rejection(self, position: VolatilityHedgePosition, starter_legs: tuple[tuple[HedgeSide, float, str], ...], prices: dict[HedgeSide, float], unit_qty: float, features: VolatilityHedgeFeatures) -> str | None:
        if unit_qty <= 0:
            return "max_contracts_per_add"
        projected = VolatilityHedgePosition(position.event_key, position.market_ticker, position.market_close_time, position.strike)
        for side, units, _ in starter_legs:
            qty = round(unit_qty * units, 10)
            projected.add_fill(side=side, price=prices[side], qty=qty, fee=qty * self.config.fee_per_contract, ts=datetime.fromisoformat("2026-01-01T00:00:00+00:00"), reason="seed_projection", projected_paired_cost=None, features=features)
        metrics = self.balancer.metrics(projected, features)
        if metrics.total_cost > self.config.max_notional_per_market + 1e-9:
            return "max_market_notional"
        if projected.up_cost > self.config.max_notional_per_side + 1e-9 or projected.down_cost > self.config.max_notional_per_side + 1e-9:
            return "max_side_notional"
        if self.config.max_worst_case_loss_per_market is not None and metrics.worst_case_pnl < -abs(self.config.max_worst_case_loss_per_market):
            return "worst_case_pnl_limit"
        return None

    def _lifecycle_allows_repair(self, balance: BalanceDecision, lifecycle: LifecycleState) -> bool:
        if lifecycle.time_to_expiry <= 0:
            return False
        if not lifecycle.repair_only or not balance.allowed:
            return False
        if balance.mode not in {PositionMode.REPAIR, PositionMode.EMERGENCY}:
            return False
        worst_case_improved = balance.projected_worst_case_pnl > balance.current_worst_case_pnl + 1e-9
        imbalance_improved = balance.projected_imbalance_ratio < balance.current_imbalance_ratio - 1e-9
        settlement_ev_improved = balance.projected_settlement_ev > balance.current_settlement_ev + 1e-9
        if lifecycle.phase == LifecyclePhase.LATE_SETTLEMENT:
            return worst_case_improved
        return worst_case_improved or imbalance_improved or settlement_ev_improved

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

    def _decision(self, event_key: str, market_ticker: str, ts: datetime, reason: str, position: VolatilityHedgePosition | None, projected: float | None, side: HedgeSide | None, features: VolatilityHedgeFeatures | None, balance: BalanceDecision | None = None, lifecycle: LifecycleState | None = None) -> VolatilityHedgeDecision:
        self._log(event_key, market_ticker, ts, side or "NONE", reason, position, projected, features, balance=balance, lifecycle=lifecycle)
        return VolatilityHedgeDecision(0, reason, [], projected, position.paired_cost if position else None, features)

    def _log(self, event_key: str, market_ticker: str, ts: datetime, side: str, reason: str, position: VolatilityHedgePosition | None, projected: float | None, features: VolatilityHedgeFeatures | None, fill: VolatilityHedgeFill | None = None, balance: BalanceDecision | None = None, lifecycle: LifecycleState | None = None, seed_gate_failed_reason: str | None = None) -> None:
        feature_dict = features.as_dict() if features else {}
        balance_json = _balance_dict(balance)
        lifecycle = lifecycle or lifecycle_for_time(float(feature_dict.get("time_to_expiry") or 0.0), self.config)
        simple_ask_sum = None
        if feature_dict.get("up_ask") is not None and feature_dict.get("down_ask") is not None:
            simple_ask_sum = round(float(feature_dict["up_ask"]) + float(feature_dict["down_ask"]), 10)
        lifecycle_fields = lifecycle.as_log_fields()
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
                "simple_ask_sum": simple_ask_sum,
                "require_seed_pair_cost_below": self.config.require_seed_pair_cost_below,
                "max_initial_ask_sum": self.config.max_initial_ask_sum,
                "seed_gate_failed_reason": seed_gate_failed_reason,
                **lifecycle_fields,
                "mode": balance_json.get("mode"),
                "allowed": balance_json.get("allowed"),
                "proposed_qty": balance_json.get("proposed_qty"),
                "final_qty": balance_json.get("final_qty"),
                "current_imbalance_ratio": balance_json.get("current_imbalance_ratio"),
                "projected_imbalance_ratio": balance_json.get("projected_imbalance_ratio"),
                "current_settlement_EV": balance_json.get("current_settlement_ev"),
                "projected_settlement_EV": balance_json.get("projected_settlement_ev"),
                "current_worst_case_pnl": balance_json.get("current_worst_case_pnl"),
                "projected_worst_case_pnl": balance_json.get("projected_worst_case_pnl"),
                "raw_json": {"fill": fill.as_dict() if fill else None, "features": feature_dict, "balance": balance_json, "lifecycle": lifecycle_fields, "seed_gate_failed_reason": seed_gate_failed_reason},
            }
        )


def _features_from_position(position: VolatilityHedgePosition) -> VolatilityHedgeFeatures:
    data = position.last_features or {}
    return VolatilityHedgeFeatures(
        time_to_expiry=float(data.get("time_to_expiry") or 0.0),
        slope=float(data.get("slope") or 0.0),
        atr=float(data.get("atr") or 0.0),
        atr_expansion_rate=float(data.get("atr_expansion_rate") or 1.0),
        distance_from_strike=float(data.get("distance_from_strike") or 0.0),
        up_bid=float(data.get("up_bid") or 0.0),
        up_ask=float(data.get("up_ask") or 0.0),
        down_bid=float(data.get("down_bid") or 0.0),
        down_ask=float(data.get("down_ask") or 0.0),
    )


def _cost_improved(current: float | None, projected: float | None, threshold: float) -> bool:
    if projected is None:
        return False
    if current is None:
        return True
    return projected <= current - threshold or (current > 0.98 and projected <= 0.98)


def lifecycle_for_time(seconds_to_close: float, config: VolatilityHedgeConfig | None = None) -> LifecycleState:
    cfg = config or VolatilityHedgeConfig()
    time_to_expiry = max(0.0, float(seconds_to_close))
    elapsed = max(0.0, min(cfg.total_contract_seconds, cfg.total_contract_seconds - time_to_expiry))
    if elapsed < cfg.observe_seconds:
        phase = LifecyclePhase.OBSERVE
        threshold = 1.00
        ask_sum = 1.00
        allow_loss = False
    elif elapsed < cfg.early_seed_until_seconds:
        phase = LifecyclePhase.EARLY_SEED
        threshold = min(cfg.require_seed_pair_cost_below, 1.03)
        ask_sum = min(cfg.max_initial_ask_sum, 1.02)
        allow_loss = True
    elif elapsed < cfg.main_harvest_until_seconds:
        phase = LifecyclePhase.MAIN_HARVEST
        threshold = cfg.require_seed_pair_cost_below
        ask_sum = cfg.max_initial_ask_sum
        allow_loss = cfg.allow_seed_loss
    elif elapsed < cfg.repair_protect_until_seconds:
        phase = LifecyclePhase.REPAIR_PROTECT
        threshold = cfg.require_seed_pair_cost_below
        ask_sum = cfg.max_initial_ask_sum
        allow_loss = cfg.allow_seed_loss
    else:
        phase = LifecyclePhase.LATE_SETTLEMENT
        threshold = 0.0
        ask_sum = 0.0
        allow_loss = False
    seed_window_open = phase in {LifecyclePhase.OBSERVE, LifecyclePhase.EARLY_SEED, LifecyclePhase.MAIN_HARVEST} and time_to_expiry > cfg.stop_new_seed_seconds_before_expiry
    normal_add_window_open = phase in {LifecyclePhase.EARLY_SEED, LifecyclePhase.MAIN_HARVEST} and time_to_expiry > cfg.stop_normal_add_seconds_before_expiry
    repair_only = not normal_add_window_open or phase in {LifecyclePhase.REPAIR_PROTECT, LifecyclePhase.LATE_SETTLEMENT}
    return LifecycleState(
        phase=phase,
        elapsed_seconds=round(elapsed, 10),
        time_to_expiry=round(time_to_expiry, 10),
        phase_seed_pair_threshold=threshold,
        phase_max_initial_ask_sum=ask_sum,
        phase_allow_seed_loss=allow_loss,
        seed_window_open=seed_window_open,
        normal_add_window_open=normal_add_window_open,
        repair_only=repair_only,
    )


def event_key_for_volatility_hedge(market_ticker: str, market_close_time: str, strike: float) -> str:
    if strike < 1_000:
        raise ValueError(f"implausible BTC strike for volatility hedge: {strike}")
    return f"{market_ticker}|{market_close_time}|{round(float(strike), 2)}"


def _balance_dict(balance: BalanceDecision | None) -> dict[str, Any]:
    if balance is None:
        return {}
    return {
        "allowed": balance.allowed,
        "reason": balance.reason,
        "mode": str(balance.mode),
        "proposed_qty": balance.proposed_qty,
        "final_qty": balance.final_qty,
        "current_imbalance_ratio": balance.current_imbalance_ratio,
        "projected_imbalance_ratio": balance.projected_imbalance_ratio,
        "current_paired_cost": balance.current_paired_cost,
        "projected_paired_cost": balance.projected_paired_cost,
        "current_settlement_ev": balance.current_settlement_ev,
        "projected_settlement_ev": balance.projected_settlement_ev,
        "current_worst_case_pnl": balance.current_worst_case_pnl,
        "projected_worst_case_pnl": balance.projected_worst_case_pnl,
        "repair_qty_needed": balance.repair_qty_needed,
    }


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
