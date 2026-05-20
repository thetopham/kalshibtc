from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .datafeed.models import OrderBookSnapshot, Tick
from .execution.paper import PaperExecutor, PaperFill
from .execution.risk import RiskDecision, RiskManager
from .market.contract import ContractWindow
from .market.state import MarketState
from .strategy.signals import Signal, Strategy


class MarketStateBuilder:
    """Build the strategy/risk state from feed + contract + analyzer output."""

    def __init__(self, *, contract: ContractWindow) -> None:
        self.contract = contract

    def from_tick_and_book(
        self,
        *,
        tick: Tick,
        orderbook: OrderBookSnapshot,
        slope_30s: float | None,
    ) -> MarketState:
        return MarketState(
            tick=tick,
            orderbook=orderbook,
            contract=self.contract,
            slope_30s=slope_30s,
        )


@dataclass(frozen=True)
class PipelineResult:
    signal: Signal
    risk: RiskDecision
    fill: PaperFill | None
    state: MarketState | None = None


class BotPipeline:
    """One-state pipeline: strategies -> risk -> executor.

    The executor is injected, so the same strategy can run in live paper,
    historical replay, or a future real Kalshi adapter without changing
    strategy code.
    """

    def __init__(
        self,
        *,
        strategies: Sequence[Strategy],
        risk_manager: RiskManager,
        executor: PaperExecutor,
        open_positions: int = 0,
    ) -> None:
        self.strategies = list(strategies)
        self.risk_manager = risk_manager
        self.executor = executor
        self.open_positions = max(0, int(open_positions))

    def on_state(self, state: MarketState) -> list[PipelineResult]:
        results: list[PipelineResult] = []
        open_positions = self.open_positions
        for strategy in self.strategies:
            signal = strategy.on_tick(state)
            risk = self.risk_manager.evaluate(state, signal, open_positions=open_positions)
            fill = self.executor.execute(state, risk)
            if fill is not None and hasattr(strategy, "on_fill"):
                strategy.on_fill(state, fill)
            if fill is not None:
                open_positions += 1
            results.append(PipelineResult(signal=signal, risk=risk, fill=fill, state=state))
        self.open_positions = open_positions
        return results
