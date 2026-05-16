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
from kalshibtc.strategy.contrarian_spread_reversion import ContrarianSpreadReversionStrategy
from kalshibtc.strategy.late_window_only import LateWindowOnlyStrategy
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


def _tick_before_close(price: float, seconds_to_close: int) -> Tick:
    return Tick(
        ts=datetime(2026, 5, 15, 12, 15, tzinfo=UTC) - timedelta(seconds=seconds_to_close),
        price=price,
        bid=price - 1,
        ask=price + 1,
        source="test",
        symbol="BTC-USD",
    )


def _book(
    yes_ask: float = 0.54,
    no_ask: float = 0.47,
    *,
    market_ticker: str = "KXBTC15M-TEST",
) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        ts=datetime(2026, 5, 15, 12, 0, 31, tzinfo=UTC),
        market_ticker=market_ticker,
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


def _state_before_close(
    price: float,
    seconds_to_close: int,
    slope: float,
    *,
    yes_bid: float = 0.52,
    yes_ask: float = 0.54,
    no_bid: float = 0.45,
    no_ask: float = 0.47,
):
    tick = _tick_before_close(price, seconds_to_close)
    book = OrderBookSnapshot(
        ts=tick.ts,
        market_ticker="KXBTC15M-TEST",
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
    )
    return MarketStateBuilder(contract=_contract()).from_tick_and_book(
        tick=tick,
        orderbook=book,
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


def test_late_window_only_requires_final_minute_wide_distance_and_decent_book() -> None:
    strategy = LateWindowOnlyStrategy()

    assert strategy.on_tick(_state_before_close(100_200.0, 90, 0.0)).reason == "outside final entry window"
    assert strategy.on_tick(_state_before_close(100_005.0, 45, 0.0)).reason == "too close to strike in late window"
    assert strategy.on_tick(_state_before_close(100_200.0, 45, 0.0, yes_ask=0.92)).reason == "late window price too expensive"
    assert strategy.on_tick(_state_before_close(100_200.0, 45, 0.0, yes_bid=0.50, yes_ask=0.58)).reason == "late window spread too wide"


def test_late_window_only_emits_single_high_conviction_side_in_final_minute() -> None:
    strategy = LateWindowOnlyStrategy()

    above = strategy.on_tick(_state_before_close(100_200.0, 45, 0.0, yes_bid=0.52, yes_ask=0.56))
    below = strategy.on_tick(_state_before_close(99_800.0, 45, 0.0, no_bid=0.52, no_ask=0.56))

    assert above == Signal(
        side="long_above",
        reason="final minute above strike with tradable book",
        confidence=pytest.approx(0.95),
        strategy="late_window_only",
    )
    assert below.side == "long_below"
    assert below.reason == "final minute below strike with tradable book"
    assert below.confidence == pytest.approx(0.95)


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


def test_replay_engine_settles_positions_at_market_rollover() -> None:
    ticks = [
        _tick(99_950.0, 0),
        _tick(99_930.0, 30),
        Tick(ts=datetime(2026, 5, 15, 12, 15, 1, tzinfo=UTC), price=99_900.0, source="test"),
        Tick(ts=datetime(2026, 5, 15, 12, 15, 2, tzinfo=UTC), price=100_080.0, source="test"),
        Tick(ts=datetime(2026, 5, 15, 12, 15, 32, tzinfo=UTC), price=100_140.0, source="test"),
    ]
    books = [
        _book(no_ask=0.47, market_ticker="KXBTC15M-A"),
        _book(no_ask=0.47, market_ticker="KXBTC15M-A"),
        _book(no_ask=0.47, market_ticker="KXBTC15M-B"),
        _book(yes_ask=0.54, market_ticker="KXBTC15M-B"),
        _book(yes_ask=0.54, market_ticker="KXBTC15M-B"),
    ]
    replay = ReplayEngine(
        config=BotConfig(),
        contract=_contract(),
        strategies=[SimpleDirectionalStrategy()],
        risk_manager=RiskManager(RiskLimits(base_size_dollars=5.0, max_open_positions=1)),
        executor=PaperExecutor(),
        settle_on_market_rollover=True,
    )

    report = replay.run(ticks=ticks, books=books)

    assert len(report.fills) == 2
    assert [fill.market_ticker for fill in report.fills] == ["KXBTC15M-A", "KXBTC15M-B"]
    assert report.settled_positions >= 1


def test_replay_engine_uses_per_book_contract_metadata_after_market_rollover() -> None:
    ticks = [
        Tick(ts=datetime(2026, 5, 15, 12, 14, 45, tzinfo=UTC), price=100_080.0, source="test"),
        Tick(ts=datetime(2026, 5, 15, 12, 15, 45, tzinfo=UTC), price=100_080.0, source="test"),
    ]
    books = [
        OrderBookSnapshot(
            ts=ticks[0].ts,
            market_ticker="KXBTC15M-A",
            yes_bid=0.52,
            yes_ask=0.54,
            no_bid=0.45,
            no_ask=0.47,
        ),
        OrderBookSnapshot(
            ts=ticks[1].ts,
            market_ticker="KXBTC15M-B",
            yes_bid=0.52,
            yes_ask=0.54,
            no_bid=0.45,
            no_ask=0.47,
            raw={"strike": 100_000.0, "market_close_time": "2026-05-15T12:30:00+00:00"},
        ),
    ]
    replay = ReplayEngine(
        config=BotConfig(),
        contract=_contract(),
        strategies=[LateWindowOnlyStrategy(max_seconds_to_close=60.0, min_distance=50.0)],
        risk_manager=RiskManager(RiskLimits(base_size_dollars=5.0, max_open_positions=10)),
        executor=PaperExecutor(),
        settle_on_market_rollover=True,
    )

    report = replay.run(ticks=ticks, books=books)

    assert report.results[0].signal.reason == "final minute above strike with tradable book"
    assert report.results[1].signal.reason == "outside final entry window"


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


def test_contrarian_spread_reversion_buys_cheap_no_when_above_strike_yes_is_expensive() -> None:
    strategy = ContrarianSpreadReversionStrategy()
    state = _state_before_close(
        100_125.0,
        seconds_to_close=420,
        slope=1.8,
        yes_bid=0.78,
        yes_ask=0.82,
        no_bid=0.19,
        no_ask=0.22,
    )

    signal = strategy.on_tick(state)

    assert signal.side == "long_below"
    assert signal.reason == "above strike: fade expensive YES by buying cheap NO"
    assert signal.confidence >= 0.60


def test_contrarian_spread_reversion_buys_cheap_yes_when_below_strike_no_is_expensive() -> None:
    strategy = ContrarianSpreadReversionStrategy()
    state = _state_before_close(
        99_875.0,
        seconds_to_close=420,
        slope=-1.8,
        yes_bid=0.17,
        yes_ask=0.20,
        no_bid=0.78,
        no_ask=0.82,
    )

    signal = strategy.on_tick(state)

    assert signal.side == "long_above"
    assert signal.reason == "below strike: fade expensive NO by buying cheap YES"
    assert signal.confidence >= 0.60


def test_contrarian_spread_reversion_rejects_wide_spread_and_final_seconds() -> None:
    strategy = ContrarianSpreadReversionStrategy(max_cheap_leg_spread=0.03, min_seconds_to_close=30)

    wide = strategy.on_tick(
        _state_before_close(
            100_125.0,
            seconds_to_close=420,
            slope=1.8,
            yes_bid=0.78,
            yes_ask=0.82,
            no_bid=0.10,
            no_ask=0.18,
        )
    )
    too_late = strategy.on_tick(
        _state_before_close(
            100_125.0,
            seconds_to_close=20,
            slope=1.8,
            yes_bid=0.78,
            yes_ask=0.82,
            no_bid=0.18,
            no_ask=0.21,
        )
    )

    assert wide.side == "none"
    assert wide.reason == "cheap hedge leg spread too wide"
    assert too_late.side == "none"
    assert too_late.reason == "too close to close for contrarian hedge"


def test_risk_allows_contrarian_cross_strike_hedge_signal() -> None:
    strategy = ContrarianSpreadReversionStrategy()
    state = _state_before_close(
        100_125.0,
        seconds_to_close=420,
        slope=1.8,
        yes_bid=0.78,
        yes_ask=0.82,
        no_bid=0.19,
        no_ask=0.22,
    )
    signal = strategy.on_tick(state)

    decision = RiskManager(RiskLimits(base_size_dollars=10.0, max_open_positions=2, max_spread=0.05)).evaluate(
        state,
        signal,
        open_positions=0,
    )

    assert decision.allowed is True
    assert decision.side == "long_below"
    assert decision.entry_price == pytest.approx(0.22)
    assert "price_not_below_strike" not in decision.blocked_by
