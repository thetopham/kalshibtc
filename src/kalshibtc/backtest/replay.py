from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

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
    settled_positions: int = 0


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
        settle_on_market_rollover: bool = False,
        fill_timing: str = "same-tick",
    ) -> None:
        self.config = config
        self.contract = contract
        self.strategies = list(strategies)
        self.risk_manager = risk_manager or RiskManager(config.risk)
        self.executor = executor or PaperExecutor()
        self.settle_on_market_rollover = settle_on_market_rollover
        if fill_timing not in {"same-tick", "next-tick"}:
            raise ValueError("fill_timing must be 'same-tick' or 'next-tick'")
        self.fill_timing = fill_timing

    def run(self, *, ticks: Sequence[Tick], books: Sequence[OrderBookSnapshot]) -> ReplayReport:
        if len(ticks) != len(books):
            raise ValueError("replay requires the same number of ticks and books")

        tracker = SlopeTracker(window_seconds=30.0)
        pipeline = BotPipeline(
            strategies=self.strategies,
            risk_manager=self.risk_manager,
            executor=self.executor,
        )
        all_results: list[PipelineResult] = []
        fills: list[PaperFill] = []
        total_signals = 0
        settled_positions = 0
        active_market_ticker: str | None = None
        active_contract = self.contract
        for tick, book in zip(ticks, books, strict=False):
            if book.market_ticker != active_contract.ticker:
                if self.settle_on_market_rollover and active_market_ticker is not None:
                    settled_positions += pipeline.open_positions
                    pipeline.open_positions = 0
                if self.fill_timing == "next-tick":
                    setattr(pipeline, "_replay_pending_decisions", [])
                active_contract = self._contract_for_book(book)
            active_market_ticker = book.market_ticker
            tracker.add(tick)
            state = MarketStateBuilder(contract=active_contract).from_tick_and_book(
                tick=tick,
                orderbook=book,
                slope_30s=tracker.velocity(),
            )
            if self.fill_timing == "next-tick":
                results = self._on_state_next_tick(pipeline, state)
            else:
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
            settled_positions=settled_positions,
        )

    def _on_state_next_tick(self, pipeline: BotPipeline, state: Any) -> list[PipelineResult]:
        pending_decisions = getattr(pipeline, "_replay_pending_decisions", [])
        results: list[PipelineResult] = []
        open_positions = pipeline.open_positions
        for decision in pending_decisions:
            if open_positions >= max(0, int(pipeline.risk_manager.limits.max_open_positions)):
                continue
            repriced_decision = self._evaluate_pending_decision_for_state(
                pipeline,
                state,
                decision,
                open_positions=open_positions,
            )
            fill = pipeline.executor.execute(state, repriced_decision)
            if fill is not None:
                for strategy in pipeline.strategies:
                    callback = getattr(strategy, "on_fill", None)
                    if strategy.name == fill.strategy and callback is not None:
                        callback(state, fill)
                        break
                if fill is None:
                    continue
                open_positions += 1
            results.append(PipelineResult(signal=decision.signal, risk=repriced_decision, fill=fill))
        pipeline.open_positions = open_positions

        next_pending = []
        for strategy in pipeline.strategies:
            signal = strategy.on_tick(state)
            risk = pipeline.risk_manager.evaluate(state, signal, open_positions=open_positions)
            if risk.allowed:
                next_pending.append(risk)
            results.append(PipelineResult(signal=signal, risk=risk, fill=None))
        setattr(pipeline, "_replay_pending_decisions", next_pending)
        return results

    def _evaluate_pending_decision_for_state(
        self,
        pipeline: BotPipeline,
        state: Any,
        decision: Any,
        *,
        open_positions: int,
    ) -> Any:
        signal = decision.signal
        repriced_decision = pipeline.risk_manager.evaluate(
            state,
            signal,
            open_positions=open_positions,
        )
        limit_price = (signal.features or {}).get("limit_price") if signal is not None else None
        if isinstance(limit_price, int | float):
            entry_price = repriced_decision.entry_price
            if entry_price is None or entry_price > float(limit_price):
                blocked_by = [*repriced_decision.blocked_by]
                if "passive_limit_not_touched" not in blocked_by:
                    blocked_by.append("passive_limit_not_touched")
                return replace(
                    repriced_decision,
                    allowed=False,
                    size_dollars=0.0,
                    entry_price=None,
                    reason="blocked by passive limit",
                    blocked_by=blocked_by,
                )
        return repriced_decision

    def _contract_for_book(self, book: OrderBookSnapshot) -> ContractWindow:
        strike = _optional_float_from_mapping(book.raw, "strike") or self.contract.strike
        close_time = _optional_datetime_from_mapping(book.raw, "market_close_time") or self.contract.close_time
        open_time = _optional_datetime_from_mapping(book.raw, "market_open_time") or self.contract.open_time
        return ContractWindow(
            ticker=book.market_ticker,
            strike=strike,
            close_time=close_time,
            open_time=open_time,
        )


def _optional_float_from_mapping(raw: dict[str, Any], key: str) -> float | None:
    value = raw.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_datetime_from_mapping(raw: dict[str, Any], key: str) -> datetime | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None
