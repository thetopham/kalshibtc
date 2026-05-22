from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProbabilityTradeConfig:
    edge_threshold: float = 0.03
    fee_rate: float = 0.0
    slippage: float = 0.0
    base_notional: float = 10.0


@dataclass(frozen=True)
class ProbabilityTradeDecision:
    side: str
    market_probability: float
    edge: float
    ev_per_contract: float
    blocked_by: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class InventorySizing:
    allowed: bool
    side: str
    notional: float
    reason: str
    blocked_by: list[str] = field(default_factory=list)


def evaluate_probability_trade(
    *,
    model_probability: float,
    yes_ask: float | None,
    no_ask: float | None,
    config: ProbabilityTradeConfig,
) -> ProbabilityTradeDecision:
    blocked: list[str] = []
    if yes_ask is None or no_ask is None or yes_ask <= 0 or no_ask <= 0:
        return ProbabilityTradeDecision("none", 0.0, 0.0, 0.0, ["missing_market_price"])
    p = min(1.0, max(0.0, float(model_probability)))
    yes_edge = p - float(yes_ask)
    no_model_probability = 1.0 - p
    no_edge = no_model_probability - float(no_ask)
    if yes_edge >= no_edge:
        side = "yes"
        market_probability = float(yes_ask)
        edge = yes_edge
        price = float(yes_ask)
        win_probability = p
    else:
        side = "no"
        market_probability = float(no_ask)
        edge = no_edge
        price = float(no_ask)
        win_probability = no_model_probability
    ev = win_probability - price - abs(config.slippage) - abs(config.fee_rate) * price
    if edge < config.edge_threshold:
        blocked.append("edge_below_threshold")
    if ev <= 0:
        blocked.append("ev_after_fee_slippage_nonpositive")
    if blocked:
        return ProbabilityTradeDecision("none", market_probability, edge, ev, blocked)
    return ProbabilityTradeDecision(side, market_probability, edge, ev, [])


@dataclass(frozen=True)
class InventoryBalancer:
    max_net_ratio: float = 0.25
    force_flatten_seconds: float = 60.0
    min_notional: float = 1.0
    venue: str = "kalshi"

    def size_order(
        self,
        *,
        desired_side: str,
        desired_notional: float,
        yes_contracts: float,
        no_contracts: float,
        yes_price: float,
        no_price: float,
        seconds_to_close: float,
    ) -> InventorySizing:
        desired_side = desired_side.lower()
        if desired_side not in {"yes", "no"}:
            return InventorySizing(False, "none", 0.0, "invalid side", ["invalid_side"])
        if self.venue.lower() == "kalshi":
            notional = float(desired_notional)
            if notional < self.min_notional:
                return InventorySizing(False, "none", 0.0, "below min notional", ["below_min_notional"])
            return InventorySizing(True, desired_side, notional, "kalshi directional probability edge sized", [])

        if seconds_to_close <= self.force_flatten_seconds and abs(yes_contracts - no_contracts) > 1e-9:
            side = "no" if yes_contracts > no_contracts else "yes"
            price = no_price if side == "no" else yes_price
            contracts_needed = abs(yes_contracts - no_contracts)
            notional = max(0.0, min(float(desired_notional), contracts_needed * price))
            return InventorySizing(notional >= self.min_notional, side, notional, "forced flatten near expiry", [] if notional >= self.min_notional else ["below_min_notional"])

        current_gross = yes_contracts + no_contracts
        if current_gross <= 0:
            notional = float(desired_notional)
            if notional < self.min_notional:
                return InventorySizing(False, "none", 0.0, "below min notional", ["below_min_notional"])
            return InventorySizing(True, desired_side, notional, "probability edge inventory sized", [])

        yes_after = yes_contracts + (desired_notional / yes_price if desired_side == "yes" and yes_price > 0 else 0.0)
        no_after = no_contracts + (desired_notional / no_price if desired_side == "no" and no_price > 0 else 0.0)
        gross_after = yes_after + no_after
        net_ratio = abs(yes_after - no_after) / gross_after if gross_after > 0 else 0.0
        if net_ratio > self.max_net_ratio:
            return InventorySizing(False, "none", 0.0, "max net ratio", ["max_net_ratio"])
        notional = float(desired_notional)
        if notional < self.min_notional:
            return InventorySizing(False, "none", 0.0, "below min notional", ["below_min_notional"])
        return InventorySizing(True, desired_side, notional, "probability edge inventory sized", [])
