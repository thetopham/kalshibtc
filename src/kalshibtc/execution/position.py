from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PositionRuleResult:
    action: str  # "hold", "exit", or "ignore"
    reason: str


class PositionRules:
    """Tiny placeholder for reusable exit/hold rules.

    Keep this separate from signal generation so a strategy can be replayed with
    different stop, take-profit, and time-exit policies later.
    """

    def on_mark(self, *, unrealized_pnl: float, stop_loss_dollars: float, take_profit_dollars: float) -> PositionRuleResult:
        if unrealized_pnl <= -abs(stop_loss_dollars):
            return PositionRuleResult("exit", "stop_loss")
        if unrealized_pnl >= abs(take_profit_dollars):
            return PositionRuleResult("exit", "take_profit")
        return PositionRuleResult("hold", "inside_rules")
