from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..config import BotConfig
from ..datafeed.models import OrderBookSnapshot, Tick
from ..execution.paper import PaperExecutor, PaperFill
from ..execution.risk import RiskManager
from ..main import BotPipeline, MarketStateBuilder, PipelineResult
from ..market.contract import ContractWindow
from ..strategy.signals import Strategy
from ..strategy.slope import SlopeTracker


@dataclass(frozen=True)
class ReplayReport:
    total_ticks: int
    total_signals: int
    fills: list[PaperFill] = field(default_factory=list)
    results: list[PipelineResult] = field(default_factory=list)


class ReplayEngine:
    """Replay 1s ticks/books through the same strategy/risk/execution seams."""

    def __init__(
        self,
        *,
        config: BotConfig,
        contract: ContractWindow,
        strategies: Sequence[Strategy],
        risk_manager: RiskManager | None = None,
        executor: PaperExecutor | None = None,
    ) -> None:
        self.config = config
        self.contract = contract
        self.strategies = list(strategies)
        self.risk_manager = risk_manager or RiskManager(config.risk)
        self.executor = executor or PaperExecutor()

    def run(self, *, ticks: Sequence[Tick], books: Sequence[OrderBookSnapshot]) -> ReplayReport:
        if len(ticks) != len(books):
            raise ValueError("replay requires the same number of ticks and books")

        tracker = SlopeTracker(window_seconds=30.0)
        builder = MarketStateBuilder(contract=self.contract)
        pipeline = BotPipeline(
            strategies=self.strategies,
            risk_manager=self.risk_manager,
            executor=self.executor,
        )
        all_results: list[PipelineResult] = []
        fills: list[PaperFill] = []
        total_signals = 0
        for tick, book in zip(ticks, books, strict=False):
            tracker.add(tick)
            state = builder.from_tick_and_book(tick=tick, orderbook=book, slope_30s=tracker.velocity())
            results = pipeline.on_state(state)
            all_results.extend(results)
            for result in results:
                if result.signal.side != "none":
                    total_signals += 1
                if result.fill is not None:
                    fills.append(result.fill)
        return ReplayReport(
            total_ticks=len(ticks),
            total_signals=total_signals,
            fills=fills,
            results=all_results,
        )
