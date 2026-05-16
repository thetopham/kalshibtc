from __future__ import annotations

from dataclasses import dataclass, field

from ..config import RiskLimits
from ..market.pricing import book_is_valid, entry_price_for_signal, spread_for_signal
from ..market.state import MarketState
from ..strategy.signals import Signal


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    side: str
    size_dollars: float
    entry_price: float | None
    reason: str
    blocked_by: list[str] = field(default_factory=list)
    signal: Signal | None = None


class RiskManager:
    """Conservative pre-execution filter and position sizing seam."""

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

    def evaluate(
        self,
        state: MarketState,
        signal: Signal,
        *,
        open_positions: int = 0,
    ) -> RiskDecision:
        blocked_by: list[str] = []
        if signal.side == "none":
            blocked_by.append("signal_none")
        elif open_positions >= max(0, int(self.limits.max_open_positions)):
            blocked_by.append("max_open_positions")
        if signal.confidence < self.limits.min_confidence:
            blocked_by.append("confidence_below_min")
        if not book_is_valid(state.orderbook):
            blocked_by.append("invalid_orderbook")
        if signal.side == "long_above" and state.price <= state.strike and "contrarian" not in signal.strategy:
            blocked_by.append("price_not_above_strike")
        if signal.side == "long_below" and state.price >= state.strike and "contrarian" not in signal.strategy:
            blocked_by.append("price_not_below_strike")

        entry_price = entry_price_for_signal(signal.side, state.orderbook)
        spread = spread_for_signal(signal.side, state.orderbook)
        if signal.side != "none" and entry_price is None:
            blocked_by.append("missing_entry_price")
        if spread is not None and spread > self.limits.max_spread:
            blocked_by.append("spread_too_wide")

        allowed = not blocked_by
        size = self.limits.clamp_size(self.limits.base_size_dollars) if allowed else 0.0
        return RiskDecision(
            allowed=allowed,
            side=signal.side,
            size_dollars=size,
            entry_price=entry_price if allowed else None,
            reason=signal.reason if allowed else "blocked by risk filter",
            blocked_by=blocked_by,
            signal=signal,
        )
