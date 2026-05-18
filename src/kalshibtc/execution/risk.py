from __future__ import annotations

from dataclasses import dataclass, field, replace

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


@dataclass(frozen=True)
class CapitalConstraints:
    starting_bankroll: float | None = None
    max_capital_at_risk: float | None = None
    fee_per_contract: float = 0.0
    fee_rate: float = 0.0
    per_market_max_exposure: float | None = None
    daily_loss_cap: float | None = None

    @property
    def enabled(self) -> bool:
        return any(
            value is not None
            for value in (
                self.starting_bankroll,
                self.max_capital_at_risk,
                self.per_market_max_exposure,
                self.daily_loss_cap,
            )
        ) or self.fee_per_contract > 0 or self.fee_rate > 0


@dataclass
class CapitalState:
    constraints: CapitalConstraints
    locked_capital: float = 0.0
    cumulative_fees: float = 0.0
    realized_pnl: float = 0.0
    max_capital_used: float = 0.0
    blocked_trades: int = 0
    market_exposure: dict[str, float] = field(default_factory=dict)
    day_pnl: dict[str, float] = field(default_factory=dict)

    @property
    def available_bankroll(self) -> float | None:
        if self.constraints.starting_bankroll is None:
            return None
        return self.constraints.starting_bankroll + self.realized_pnl - self.locked_capital

    def fee_for(self, *, notional: float, contracts: float) -> float:
        return max(0.0, abs(contracts) * self.constraints.fee_per_contract + abs(notional) * self.constraints.fee_rate)

    def adjusted_decision(self, state: MarketState, decision: RiskDecision) -> RiskDecision:
        if not decision.allowed or decision.entry_price is None or decision.size_dollars <= 0:
            return decision
        blocked_by = [*decision.blocked_by]
        notional = float(decision.size_dollars)
        contracts = notional / decision.entry_price if decision.entry_price > 0 else 0.0
        fee = self.fee_for(notional=notional, contracts=contracts)
        required = notional + fee
        available = self.available_bankroll
        if available is not None and required > max(0.0, available):
            blocked_by.append("bankroll_exhausted")
        max_risk = self.constraints.max_capital_at_risk
        if max_risk is not None and self.locked_capital + required > max(0.0, max_risk):
            blocked_by.append("capital_at_risk_limit")
        per_market = self.constraints.per_market_max_exposure
        current_market = self.market_exposure.get(state.contract.ticker, 0.0)
        if per_market is not None and current_market + required > max(0.0, per_market):
            blocked_by.append("per_market_exposure_limit")
        day = state.tick.ts.date().isoformat()
        daily_loss_cap = self.constraints.daily_loss_cap
        if daily_loss_cap is not None and self.day_pnl.get(day, 0.0) <= -abs(daily_loss_cap):
            blocked_by.append("daily_loss_cap")
        if not blocked_by:
            return decision
        self.blocked_trades += 1
        return replace(
            decision,
            allowed=False,
            size_dollars=0.0,
            entry_price=None,
            reason="blocked by capital constraints",
            blocked_by=blocked_by,
        )

    def on_fill(self, fill: object) -> None:
        notional = float(getattr(fill, "notional", 0.0) or 0.0)
        contracts = float(getattr(fill, "contracts", 0.0) or 0.0)
        fee = self.fee_for(notional=notional, contracts=contracts)
        self.locked_capital += notional + fee
        self.cumulative_fees += fee
        self.max_capital_used = max(self.max_capital_used, self.locked_capital)
        market = str(getattr(fill, "market_ticker", ""))
        self.market_exposure[market] = self.market_exposure.get(market, 0.0) + notional + fee

    def settle_market(self, market_ticker: str, *, payout: float = 0.0, day: str | None = None) -> None:
        released = self.market_exposure.pop(market_ticker, 0.0)
        if released <= 0:
            return
        self.locked_capital = max(0.0, self.locked_capital - released)
        pnl = float(payout) - released
        self.realized_pnl += pnl
        if day:
            self.day_pnl[day] = self.day_pnl.get(day, 0.0) + pnl

    def metrics(self) -> dict[str, float | int | None]:
        starting = self.constraints.starting_bankroll
        ending = None if starting is None else starting + self.realized_pnl - self.cumulative_fees
        return {
            "starting_bankroll": starting,
            "ending_bankroll": ending,
            "max_capital_used": round(self.max_capital_used, 6),
            "return_on_max_capital_used": round((self.realized_pnl - self.cumulative_fees) / self.max_capital_used, 6) if self.max_capital_used > 0 else None,
            "cumulative_fees": round(self.cumulative_fees, 6),
            "realized_pnl_after_fees": round(self.realized_pnl - self.cumulative_fees, 6),
            "blocked_trades": self.blocked_trades,
            "locked_capital_end": round(self.locked_capital, 6),
        }


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
        if not signal.allow_price_strike_mismatch:
            if signal.side == "long_above" and state.price <= state.strike:
                blocked_by.append("price_not_above_strike")
            if signal.side == "long_below" and state.price >= state.strike:
                blocked_by.append("price_not_below_strike")

        entry_price = entry_price_for_signal(signal.side, state.orderbook)
        spread = spread_for_signal(signal.side, state.orderbook)
        if signal.side != "none" and entry_price is None:
            blocked_by.append("missing_entry_price")
        if spread is not None and spread > self.limits.max_spread:
            blocked_by.append("spread_too_wide")

        allowed = not blocked_by
        requested_size = signal.target_notional if signal.target_notional is not None else self.limits.base_size_dollars
        size = self.limits.clamp_size(requested_size) if allowed else 0.0
        return RiskDecision(
            allowed=allowed,
            side=signal.side,
            size_dollars=size,
            entry_price=entry_price if allowed else None,
            reason=signal.reason if allowed else "blocked by risk filter",
            blocked_by=blocked_by,
            signal=signal,
        )
