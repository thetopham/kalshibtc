from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..market.state import MarketState
from .risk import RiskDecision


@dataclass(frozen=True)
class PaperFill:
    strategy: str
    market_ticker: str
    side: str
    entry_price: float
    notional: float
    contracts: float
    ts: datetime
    mode: str = "paper"


class PaperExecutor:
    """Fake fill adapter; no network and no Kalshi side effects."""

    def execute(self, state: MarketState, decision: RiskDecision) -> PaperFill | None:
        if not decision.allowed or decision.entry_price is None or decision.entry_price <= 0:
            return None
        strategy = decision.signal.strategy if decision.signal is not None else "unknown"
        return PaperFill(
            strategy=strategy,
            market_ticker=state.contract.ticker,
            side=decision.side,
            entry_price=decision.entry_price,
            notional=decision.size_dollars,
            contracts=decision.size_dollars / decision.entry_price,
            ts=state.tick.ts,
        )
