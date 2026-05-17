from __future__ import annotations

import logging
from dataclasses import dataclass

from ..market.state import MarketState
from ..portfolio.hedge_position import HedgePosition

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HedgeVolatilityConfig:
    # Backwards-compatible alias: if target_pair_cost is not provided,
    # max_projected_pair_cost is used as the add-phase target.
    max_projected_pair_cost: float = 0.98
    target_pair_cost: float | None = None
    seed_max_pair_cost: float = 1.10
    min_abs_slope: float = 0.0
    min_recent_volatility: float = 0.0
    min_distance_from_strike: float = 0.0
    min_seconds_to_expiry: float = 45.0
    max_seconds_to_expiry: float = 14.5 * 60.0
    max_leg_ask: float = 0.95
    trend_contracts: float = 3.0
    countertrend_contracts: float = 2.0
    add_contracts: float = 1.0
    max_contracts_per_market: float = 25.0
    max_imbalance_ratio: float = 1.5
    rebalance_after_target: bool = True
    max_unpaired_contracts: float = 2.0
    prefer_smaller_side_when_paired_cost_below_target: bool = True
    cheaper_side_only_adds: bool = False
    rebalance_seconds_to_expiry: float = 180.0
    force_balance_near_expiry: bool = True
    allow_balance_add_above_target: bool = True
    max_balance_add_pair_cost: float = 1.02
    target_final_unpaired_contracts: float = 0.0

    @property
    def add_target_pair_cost(self) -> float:
        return self.target_pair_cost if self.target_pair_cost is not None else self.max_projected_pair_cost


@dataclass(frozen=True)
class HedgeDecision:
    side: str
    contracts: float
    price: float
    reason: str
    projected_combined_average_cost: float


class HedgeVolatilityV0:
    """Boring paper-only synthetic YES/NO inventory strategy.

    It never submits live orders. It only proposes paper fills against current
    executable asks and relies on the paper executor to record them.
    """

    name = "hedge_volatility_v0"

    def __init__(self, config: HedgeVolatilityConfig | None = None) -> None:
        self.config = config or HedgeVolatilityConfig()

    def decide(
        self,
        state: MarketState,
        position: HedgePosition,
        *,
        recent_volatility: float | None = None,
    ) -> list[HedgeDecision]:
        gate = self._gate(state, recent_volatility=recent_volatility)
        trend_side = self._trend_side(state)
        if gate is not None:
            self._log("REJECT", state, reason=gate, trend_side=trend_side, position=position)
            return []
        if state.orderbook.yes_ask is None or state.orderbook.no_ask is None:
            self._log("REJECT", state, reason="missing_executable_ask", trend_side=trend_side, position=position)
            return []
        if state.orderbook.yes_ask > self.config.max_leg_ask or state.orderbook.no_ask > self.config.max_leg_ask:
            self._log("REJECT", state, reason="leg_ask_too_high", trend_side=trend_side, position=position)
            return []

        if position.yes_contracts == 0.0 and position.no_contracts == 0.0:
            return self._seed_decisions(state, position=position, trend_side=trend_side)
        if self._is_expiry_rebalance_window(state):
            return self._expiry_rebalance_decisions(state, position=position, trend_side=trend_side)
        return self._add_decisions(state, position=position, trend_side=trend_side)

    def _seed_decisions(
        self,
        state: MarketState,
        *,
        position: HedgePosition,
        trend_side: str,
    ) -> list[HedgeDecision]:
        assert state.orderbook.yes_ask is not None
        assert state.orderbook.no_ask is not None
        projected_cost = state.orderbook.yes_ask + state.orderbook.no_ask
        if projected_cost > self.config.seed_max_pair_cost:
            self._log(
                "REJECT",
                state,
                reason="seed_pair_cost_too_high",
                trend_side=trend_side,
                position=position,
                seed_pair_cost=projected_cost,
                seed_max_pair_cost=self.config.seed_max_pair_cost,
            )
            return []
        yes_contracts, no_contracts = self._seed_contracts(trend_side)
        reason = f"seed_3_to_2_trend_{trend_side}"
        decisions = [
            HedgeDecision("yes", yes_contracts, state.orderbook.yes_ask, reason, projected_cost),
            HedgeDecision("no", no_contracts, state.orderbook.no_ask, reason, projected_cost),
        ]
        for decision in decisions:
            self._log(
                "ALLOW",
                state,
                reason=reason,
                trend_side=trend_side,
                position=position,
                side=decision.side,
                contracts=decision.contracts,
                price=decision.price,
                projected_combined_average_cost=projected_cost,
                seed_pair_cost=projected_cost,
            )
        return decisions

    def _add_decisions(
        self,
        state: MarketState,
        *,
        position: HedgePosition,
        trend_side: str,
    ) -> list[HedgeDecision]:
        assert state.orderbook.yes_ask is not None
        assert state.orderbook.no_ask is not None
        decisions: list[HedgeDecision] = []
        current_cost = position.combined_average_cost
        for side, price in (("yes", state.orderbook.yes_ask), ("no", state.orderbook.no_ask)):
            if self.config.cheaper_side_only_adds:
                other_price = state.orderbook.no_ask if side == "yes" else state.orderbook.yes_ask
                if other_price is not None and price > other_price:
                    self._log(
                        "REJECT",
                        state,
                        reason="not_cheaper_side",
                        trend_side=trend_side,
                        position=position,
                        side=side,
                        price=price,
                        other_price=other_price,
                    )
                    continue
            if not position.would_improve(side=side, price=price):
                self._log(
                    "REJECT",
                    state,
                    reason=f"{side}_price_not_improved",
                    trend_side=trend_side,
                    position=position,
                    side=side,
                    price=price,
                )
                continue
            projected_cost = position.projected_combined_average_cost(
                side=side,
                price=price,
                contracts=self.config.add_contracts,
            )
            if projected_cost is None:
                continue
            if projected_cost > self.config.add_target_pair_cost:
                self._log(
                    "REJECT",
                    state,
                    reason="projected_combined_cost_too_high",
                    trend_side=trend_side,
                    position=position,
                    side=side,
                    price=price,
                    projected_combined_average_cost=projected_cost,
                    target_pair_cost=self.config.add_target_pair_cost,
                )
                continue
            if current_cost is not None and projected_cost >= current_cost:
                self._log(
                    "REJECT",
                    state,
                    reason="projected_combined_cost_not_improved",
                    trend_side=trend_side,
                    position=position,
                    side=side,
                    price=price,
                    projected_combined_average_cost=projected_cost,
                )
                continue
            projected_yes = position.yes_contracts + (self.config.add_contracts if side == "yes" else 0.0)
            projected_no = position.no_contracts + (self.config.add_contracts if side == "no" else 0.0)
            if projected_yes + projected_no > self.config.max_contracts_per_market:
                self._log(
                    "REJECT",
                    state,
                    reason="max_contracts_per_market_reached",
                    trend_side=trend_side,
                    position=position,
                    side=side,
                    price=price,
                )
                continue
            projected_unpaired = abs(projected_yes - projected_no)
            if projected_unpaired > self.config.max_unpaired_contracts:
                self._log(
                    "REJECT",
                    state,
                    reason="max_unpaired_contracts_exceeded",
                    trend_side=trend_side,
                    position=position,
                    side=side,
                    price=price,
                    projected_yes_contracts=projected_yes,
                    projected_no_contracts=projected_no,
                    projected_unpaired_contracts=projected_unpaired,
                    max_unpaired_contracts=self.config.max_unpaired_contracts,
                )
                continue
            if _imbalance_ratio(projected_yes, projected_no) > self.config.max_imbalance_ratio:
                self._log(
                    "REJECT",
                    state,
                    reason="max_imbalance_ratio_exceeded",
                    trend_side=trend_side,
                    position=position,
                    side=side,
                    price=price,
                    projected_yes_contracts=projected_yes,
                    projected_no_contracts=projected_no,
                    max_imbalance_ratio=self.config.max_imbalance_ratio,
                )
                continue
            if self._would_increase_directional_imbalance(
                side=side,
                position=position,
                current_cost=current_cost,
            ):
                self._log(
                    "REJECT",
                    state,
                    reason="would_increase_directional_imbalance",
                    trend_side=trend_side,
                    position=position,
                    side=side,
                    price=price,
                    projected_yes_contracts=projected_yes,
                    projected_no_contracts=projected_no,
                    max_unpaired_contracts=self.config.max_unpaired_contracts,
                )
                continue
            reason = f"add_improves_combined_cost_trend_{trend_side}"
            decision = HedgeDecision(
                side=side,
                contracts=self.config.add_contracts,
                price=price,
                reason=reason,
                projected_combined_average_cost=projected_cost,
            )
            decisions.append(decision)
            self._log(
                "ALLOW",
                state,
                reason=reason,
                trend_side=trend_side,
                position=position,
                side=side,
                contracts=decision.contracts,
                price=price,
                projected_combined_average_cost=projected_cost,
            )
        return decisions

    def _expiry_rebalance_decisions(
        self,
        state: MarketState,
        *,
        position: HedgePosition,
        trend_side: str,
    ) -> list[HedgeDecision]:
        assert state.orderbook.yes_ask is not None
        assert state.orderbook.no_ask is not None
        if not self.config.force_balance_near_expiry:
            return self._add_decisions(state, position=position, trend_side=trend_side)
        unpaired = abs(position.yes_contracts - position.no_contracts)
        if unpaired <= self.config.target_final_unpaired_contracts:
            self._log(
                "REJECT",
                state,
                reason="no_expiry_balance_needed",
                trend_side=trend_side,
                position=position,
            )
            return []
        if position.yes_contracts == position.no_contracts:
            return []

        smaller_side = "yes" if position.yes_contracts < position.no_contracts else "no"
        larger_side = "no" if smaller_side == "yes" else "yes"
        price = state.orderbook.yes_ask if smaller_side == "yes" else state.orderbook.no_ask
        larger_price = state.orderbook.no_ask if larger_side == "no" else state.orderbook.yes_ask

        self._log(
            "REJECT",
            state,
            reason="expiry_balance_reject_larger_side",
            trend_side=trend_side,
            position=position,
            side=larger_side,
            price=larger_price,
        )
        projected_cost = position.projected_combined_average_cost(
            side=smaller_side,
            price=price,
            contracts=self.config.add_contracts,
        )
        if projected_cost is None:
            return []
        max_pair_cost = (
            self.config.max_balance_add_pair_cost
            if self.config.allow_balance_add_above_target
            else self.config.add_target_pair_cost
        )
        if projected_cost > max_pair_cost:
            self._log(
                "REJECT",
                state,
                reason="expiry_balance_pair_cost_too_high",
                trend_side=trend_side,
                position=position,
                side=smaller_side,
                price=price,
                projected_combined_average_cost=projected_cost,
                target_pair_cost=self.config.add_target_pair_cost,
                max_balance_add_pair_cost=max_pair_cost,
            )
            return []

        projected_yes = position.yes_contracts + (self.config.add_contracts if smaller_side == "yes" else 0.0)
        projected_no = position.no_contracts + (self.config.add_contracts if smaller_side == "no" else 0.0)
        if projected_yes + projected_no > self.config.max_contracts_per_market:
            self._log(
                "REJECT",
                state,
                reason="max_contracts_per_market_reached",
                trend_side=trend_side,
                position=position,
                side=smaller_side,
                price=price,
            )
            return []

        decision = HedgeDecision(
            side=smaller_side,
            contracts=self.config.add_contracts,
            price=price,
            reason="expiry_balance_add_smaller_side",
            projected_combined_average_cost=projected_cost,
        )
        self._log(
            "ALLOW",
            state,
            reason=decision.reason,
            trend_side=trend_side,
            position=position,
            side=decision.side,
            contracts=decision.contracts,
            price=decision.price,
            projected_combined_average_cost=projected_cost,
            projected_yes_contracts=projected_yes,
            projected_no_contracts=projected_no,
        )
        return [decision]

    def _is_expiry_rebalance_window(self, state: MarketState) -> bool:
        return (
            self.config.force_balance_near_expiry
            and state.seconds_to_close <= self.config.rebalance_seconds_to_expiry
        )

    def _would_increase_directional_imbalance(
        self,
        *,
        side: str,
        position: HedgePosition,
        current_cost: float | None,
    ) -> bool:
        if not self.config.rebalance_after_target:
            return False
        if not self.config.prefer_smaller_side_when_paired_cost_below_target:
            return False
        if current_cost is None or current_cost > self.config.add_target_pair_cost:
            return False
        yes_contracts = position.yes_contracts
        no_contracts = position.no_contracts
        unpaired = abs(yes_contracts - no_contracts)
        if unpaired <= self.config.max_unpaired_contracts:
            return False
        larger_side = "yes" if yes_contracts > no_contracts else "no"
        return side == larger_side

    def _gate(self, state: MarketState, *, recent_volatility: float | None) -> str | None:
        if self._trend_side(state) == "flat":
            return "flat_trend"
        if abs(state.slope_30s or 0.0) < self.config.min_abs_slope:
            return "slope_too_small"
        if abs(state.distance_from_strike) < self.config.min_distance_from_strike:
            return "distance_too_small"
        if state.seconds_to_close < self.config.min_seconds_to_expiry:
            return "too_close_to_expiry"
        if state.seconds_to_close > self.config.max_seconds_to_expiry:
            return "too_early_for_contract"
        if recent_volatility is not None and recent_volatility < self.config.min_recent_volatility:
            return "recent_volatility_too_small"
        return None

    def _seed_contracts(self, trend_side: str) -> tuple[float, float]:
        if trend_side == "yes":
            return self.config.trend_contracts, self.config.countertrend_contracts
        return self.config.countertrend_contracts, self.config.trend_contracts

    def _trend_side(self, state: MarketState) -> str:
        slope = state.slope_30s or 0.0
        if slope > 0.0:
            return "yes"
        if slope < 0.0:
            return "no"
        return "flat"

    def _log(
        self,
        decision: str,
        state: MarketState,
        *,
        reason: str,
        trend_side: str,
        position: HedgePosition,
        **fields: object,
    ) -> None:
        extra = " ".join(f"{key}={value}" for key, value in fields.items())
        logger.info(
            "hedge_volatility_v0 decision=%s market=%s ts=%s reason=%s trend_side=%s "
            "yes_contracts=%s no_contracts=%s combined_average_cost=%s %s",
            decision,
            state.orderbook.market_ticker,
            state.tick.ts.isoformat(),
            reason,
            trend_side,
            position.yes_contracts,
            position.no_contracts,
            position.combined_average_cost,
            extra,
        )


def _imbalance_ratio(yes_contracts: float, no_contracts: float) -> float:
    smaller = min(yes_contracts, no_contracts)
    larger = max(yes_contracts, no_contracts)
    if larger <= 0.0:
        return 0.0
    if smaller <= 0.0:
        return float("inf")
    return larger / smaller
