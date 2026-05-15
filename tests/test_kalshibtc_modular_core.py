from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kalshibtc.backtest.metrics import compute_metrics
from kalshibtc.backtest.replay import ReplayEngine
from kalshibtc.config import BotConfig, RiskLimits
from kalshibtc.datafeed.models import OrderBookSnapshot, Tick
from kalshibtc.execution.kalshi import KalshiBrokerAdapter
from kalshibtc.execution.paper import PaperExecutor
from kalshibtc.execution.risk import RiskManager
from kalshibtc.main import BotPipeline, MarketStateBuilder
from kalshibtc.market.contract import ContractWindow
from kalshibtc.strategy.signals import Signal
from kalshibtc.strategy.simple_directional import SimpleDirectionalStrategy
from kalshibtc.strategy.slope import SlopeTracker


def _tick(price: float, seconds: int) -> Tick:
    return Tick(
        ts=datetime(2026, 5, 15, 12, 0, seconds, tzinfo=UTC),
        price=price,
        bid=price - 1,
        ask=price + 1,
        source="test",
        symbol="BTC-USD",
    )


def _book(yes_ask: float = 0.54, no_ask: float = 0.47) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        ts=datetime(2026, 5, 15, 12, 0, 31, tzinfo=UTC),
        market_ticker="KXBTC15M-TEST",
        yes_bid=0.52,
        yes_ask=yes_ask,
        no_bid=0.45,
        no_ask=no_ask,
    )


def _contract() -> ContractWindow:
    return ContractWindow(
        ticker="KXBTC15M-TEST",
        strike=100_000.0,
        close_time=datetime(2026, 5, 15, 12, 15, tzinfo=UTC),
    )


def _state(price: float, slope: float, *, yes_ask: float = 0.54, no_ask: float = 0.47):
    builder = MarketStateBuilder(contract=_contract())
    return builder.from_tick_and_book(
        tick=_tick(price, 31),
        orderbook=_book(yes_ask=yes_ask, no_ask=no_ask),
        slope_30s=slope,
    )


def test_simple_directional_strategy_returns_long_above_when_price_above_and_slope_up() -> None:
    signal = SimpleDirectionalStrategy().on_tick(_state(100_125.0, 4.2))

    assert signal == Signal(
        side="long_above",
        reason="above strike + trend up",
        confidence=pytest.approx(0.542),
        strategy="simple_directional",
    )


def test_simple_directional_strategy_returns_long_below_when_price_below_and_slope_down() -> None:
    signal = SimpleDirectionalStrategy().on_tick(_state(99_875.0, -3.0))

    assert signal.side == "long_below"
    assert signal.reason == "below strike + trend down"
    assert signal.confidence == pytest.approx(0.53)


def test_simple_directional_strategy_returns_none_when_strike_and_slope_do_not_align() -> None:
    signal = SimpleDirectionalStrategy().on_tick(_state(100_125.0, -3.0))

    assert signal == Signal(side="none", reason="no alignment", confidence=0.0, strategy="simple_directional")


def test_slope_tracker_uses_older_window_tick_for_velocity() -> None:
    tracker = SlopeTracker(window_seconds=30)
    tracker.add(_tick(100_000.0, 0))
    tracker.add(_tick(100_009.0, 10))
    tracker.add(_tick(100_060.0, 30))

    assert tracker.velocity() == pytest.approx(2.0)


def test_risk_manager_sizes_allowed_signal_without_executing_it() -> None:
    risk = RiskManager(RiskLimits(base_size_dollars=25.0, max_position_dollars=40.0, max_spread=0.05))

    decision = risk.evaluate(_state(100_125.0, 4.2, yes_ask=0.54), Signal("long_above", "above strike + trend up", 0.80))

    assert decision.allowed is True
    assert decision.size_dollars == pytest.approx(25.0)
    assert decision.entry_price == pytest.approx(0.54)
    assert decision.blocked_by == []


def test_risk_manager_blocks_none_signal_and_wide_spreads() -> None:
    risk = RiskManager(RiskLimits(base_size_dollars=25.0, max_position_dollars=40.0, max_spread=0.02))

    no_signal = risk.evaluate(_state(100_125.0, 4.2), Signal("none", "no alignment", 0.0))
    wide = risk.evaluate(_state(100_125.0, 4.2, yes_ask=0.60), Signal("long_above", "above strike + trend up", 0.80))

    assert no_signal.allowed is False
    assert no_signal.blocked_by == ["signal_none"]
    assert wide.allowed is False
    assert "spread_too_wide" in wide.blocked_by


def test_paper_executor_fills_from_risk_decision_and_never_calls_kalshi() -> None:
    state = _state(100_125.0, 4.2)
    risk_decision = RiskManager(RiskLimits(base_size_dollars=25.0)).evaluate(
        state,
        Signal("long_above", "above strike + trend up", 0.80),
    )

    fill = PaperExecutor().execute(state, risk_decision)

    assert fill is not None
    assert fill.side == "long_above"
    assert fill.entry_price == pytest.approx(0.54)
    assert fill.contracts == pytest.approx(25.0 / 0.54)
    assert fill.mode == "paper"


def test_kalshi_broker_adapter_is_disabled_by_default() -> None:
    result = KalshiBrokerAdapter(enabled=False).submit(_state(100_125.0, 4.2), Signal("long_above", "x", 0.8))

    assert result.submitted is False
    assert result.reason == "live_orders_disabled"


def test_pipeline_can_run_multiple_strategies_over_one_state() -> None:
    pipeline = BotPipeline(
        strategies=[SimpleDirectionalStrategy(name="simple_a"), SimpleDirectionalStrategy(name="simple_b")],
        risk_manager=RiskManager(RiskLimits(base_size_dollars=10.0, max_open_positions=2)),
        executor=PaperExecutor(),
    )

    results = pipeline.on_state(_state(100_125.0, 4.2))

    assert [result.signal.strategy for result in results] == ["simple_a", "simple_b"]
    assert all(result.risk.allowed for result in results)
    assert [result.fill.notional for result in results if result.fill] == [10.0, 10.0]


def test_pipeline_enforces_max_open_positions_across_strategies() -> None:
    pipeline = BotPipeline(
        strategies=[SimpleDirectionalStrategy(name="simple_a"), SimpleDirectionalStrategy(name="simple_b")],
        risk_manager=RiskManager(RiskLimits(base_size_dollars=10.0, max_open_positions=1)),
        executor=PaperExecutor(),
    )

    results = pipeline.on_state(_state(100_125.0, 4.2))

    assert [result.fill is not None for result in results] == [True, False]
    assert results[1].risk.blocked_by == ["max_open_positions"]


def test_replay_engine_reuses_same_strategy_without_live_execution() -> None:
    ticks = [_tick(99_950.0, 0), _tick(99_975.0, 10), _tick(100_030.0, 30), _tick(100_060.0, 31)]
    books = [_book(yes_ask=0.53, no_ask=0.48)] * len(ticks)
    replay = ReplayEngine(
        config=BotConfig(),
        contract=_contract(),
        strategies=[SimpleDirectionalStrategy()],
        risk_manager=RiskManager(RiskLimits(base_size_dollars=5.0)),
        executor=PaperExecutor(),
    )

    report = replay.run(ticks=ticks, books=books)

    assert report.total_ticks == 4
    assert report.total_signals >= 1
    assert report.fills[-1].mode == "paper"
    assert report.fills[-1].side == "long_above"


def test_replay_engine_rejects_mismatched_tick_and_book_lengths() -> None:
    replay = ReplayEngine(
        config=BotConfig(),
        contract=_contract(),
        strategies=[SimpleDirectionalStrategy()],
        risk_manager=RiskManager(RiskLimits(base_size_dollars=5.0)),
        executor=PaperExecutor(),
    )

    with pytest.raises(ValueError, match="same number of ticks and books"):
        replay.run(ticks=[_tick(100_000.0, 0), _tick(100_001.0, 1)], books=[_book()])


def test_metrics_report_win_rate_ev_and_drawdown_for_strategy_fills() -> None:
    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    fills = [
        {"strategy": "a", "pnl": 4.0, "closed_at": now},
        {"strategy": "a", "pnl": -1.0, "closed_at": now + timedelta(seconds=1)},
        {"strategy": "a", "pnl": 2.0, "closed_at": now + timedelta(seconds=2)},
    ]

    metrics = compute_metrics(fills)

    assert metrics["trades"] == 3
    assert metrics["win_rate"] == pytest.approx(2 / 3)
    assert metrics["ev_per_trade"] == pytest.approx(5 / 3)
    assert metrics["max_drawdown"] == pytest.approx(1.0)
