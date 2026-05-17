from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kalshibtc.backtest.replay import ReplayEngine
from kalshibtc.config import BotConfig, RiskLimits
from kalshibtc.datafeed.models import OrderBookSnapshot, Tick
from kalshibtc.execution.paper import PaperExecutor
from kalshibtc.execution.risk import RiskManager
from kalshibtc.main import MarketStateBuilder
from kalshibtc.market.contract import ContractWindow
from kalshibtc.strategy.simple_inventory_mm import SimpleInventoryMMConfig, SimpleInventoryMMStrategy


def _contract() -> ContractWindow:
    return ContractWindow(
        ticker="KXBTC15M-MM",
        strike=100_000.0,
        open_time=datetime(2026, 5, 15, 12, 0, tzinfo=UTC),
        close_time=datetime(2026, 5, 15, 12, 15, tzinfo=UTC),
    )


def _state(*, seconds_to_close: int, price: float, yes_ask: float, no_ask: float):
    ts = _contract().close_time - timedelta(seconds=seconds_to_close)
    return MarketStateBuilder(contract=_contract()).from_tick_and_book(
        tick=Tick(ts=ts, price=price, source="test"),
        orderbook=OrderBookSnapshot(
            ts=ts,
            market_ticker="KXBTC15M-MM",
            yes_bid=max(0.001, yes_ask - 0.08),
            yes_ask=yes_ask,
            no_bid=max(0.001, no_ask - 0.08),
            no_ask=no_ask,
        ),
        slope_30s=0.0,
    )


def test_simple_inventory_seeds_balanced_inventory_with_passive_limits() -> None:
    strategy = SimpleInventoryMMStrategy(
        SimpleInventoryMMConfig(seed_contracts_per_side=10, seed_add_contracts=10, seed_limit_price=0.58)
    )

    yes = strategy.on_tick(_state(seconds_to_close=600, price=100_010, yes_ask=0.56, no_ask=0.55))
    assert yes.side == "long_above"
    assert yes.target_notional == pytest.approx(5.6)
    state = _state(seconds_to_close=599, price=100_010, yes_ask=0.56, no_ask=0.55)
    fill = PaperExecutor().execute(
        state,
        RiskManager(RiskLimits(max_spread=1.0, min_confidence=0.0, max_position_dollars=100)).evaluate(state, yes),
    )
    assert fill is not None
    strategy.on_fill(state, fill)

    no = strategy.on_tick(_state(seconds_to_close=593, price=99_990, yes_ask=0.60, no_ask=0.55))
    assert no.side == "long_below"
    assert no.reason == "seed balanced no inventory at passive limit"


def test_simple_inventory_averages_down_collapsed_side_then_allows_small_rich_add() -> None:
    strategy = SimpleInventoryMMStrategy(
        SimpleInventoryMMConfig(
            seed_contracts_per_side=0,
            cheap_limit_price=0.35,
            cheap_add_contracts=12,
            rich_add_contracts=4,
            rich_limit_price=0.82,
            max_net_contracts=100,
            min_improvement_cents=-1.0,
        )
    )
    strategy._yes.qty = 56
    strategy._yes.avg_price = 0.56
    strategy._no.qty = 46
    strategy._no.avg_price = 0.46
    strategy._active_market_ticker = "KXBTC15M-MM"

    cheap = strategy.on_tick(_state(seconds_to_close=420, price=100_300, yes_ask=0.77, no_ask=0.25))
    assert cheap.side == "long_below"
    assert cheap.reason == "average down collapsed side at passive limit"
    assert cheap.estimated_shares == pytest.approx(12)
    state = _state(seconds_to_close=419, price=100_300, yes_ask=0.77, no_ask=0.25)
    fill = PaperExecutor().execute(
        state,
        RiskManager(RiskLimits(max_spread=1.0, min_confidence=0.0, max_position_dollars=100)).evaluate(state, cheap),
    )
    assert fill is not None
    strategy.on_fill(state, fill)

    rich = strategy.on_tick(_state(seconds_to_close=413, price=100_300, yes_ask=0.77, no_ask=0.39))
    assert rich.side == "long_above"
    assert rich.reason == "light rich-side add using freed inventory capacity"
    assert rich.estimated_shares == pytest.approx(4)


def test_simple_inventory_blocks_runaway_net_exposure() -> None:
    strategy = SimpleInventoryMMStrategy(SimpleInventoryMMConfig(seed_contracts_per_side=0, max_net_contracts=10))
    strategy._yes.qty = 20
    strategy._yes.avg_price = 0.56
    strategy._no.qty = 5
    strategy._no.avg_price = 0.30
    strategy._active_market_ticker = "KXBTC15M-MM"

    signal = strategy.on_tick(_state(seconds_to_close=420, price=100_300, yes_ask=0.77, no_ask=0.25))

    assert signal.side == "none"
    assert signal.reason == "no passive inventory limit touched"


def test_simple_inventory_replay_writes_fills_without_same_tick_when_configured() -> None:
    close = _contract().close_time
    ticks = [
        Tick(ts=close - timedelta(seconds=600), price=100_000, source="test"),
        Tick(ts=close - timedelta(seconds=599), price=100_001, source="test"),
        Tick(ts=close - timedelta(seconds=598), price=100_002, source="test"),
    ]
    books = [
        OrderBookSnapshot(ts=ticks[0].ts, market_ticker="KXBTC15M-MM", yes_bid=0.48, yes_ask=0.56, no_bid=0.47, no_ask=0.55),
        OrderBookSnapshot(ts=ticks[1].ts, market_ticker="KXBTC15M-MM", yes_bid=0.47, yes_ask=0.55, no_bid=0.47, no_ask=0.55),
        OrderBookSnapshot(ts=ticks[2].ts, market_ticker="KXBTC15M-MM", yes_bid=0.54, yes_ask=0.62, no_bid=0.47, no_ask=0.55),
    ]
    strategy = SimpleInventoryMMStrategy(SimpleInventoryMMConfig(seed_contracts_per_side=10, seed_add_contracts=10))
    replay = ReplayEngine(
        config=BotConfig(),
        contract=_contract(),
        strategies=[strategy],
        risk_manager=RiskManager(RiskLimits(base_size_dollars=100, max_position_dollars=100, max_spread=1.0, max_open_positions=10, min_confidence=0.0)),
        executor=PaperExecutor(),
        fill_timing="next-tick",
    )

    report = replay.run(ticks=ticks, books=books)

    assert report.fills
    assert report.fills[0].ts == ticks[1].ts
    assert strategy.position_history


def test_simple_inventory_final_window_repairs_smaller_side_only() -> None:
    strategy = SimpleInventoryMMStrategy(
        SimpleInventoryMMConfig(seed_contracts_per_side=0, repair_window_seconds=180, repair_max_price=0.90, repair_add_contracts=12)
    )
    strategy._yes.qty = 56
    strategy._yes.avg_price = 0.56
    strategy._no.qty = 33
    strategy._no.avg_price = 0.33
    strategy._active_market_ticker = "KXBTC15M-MM"

    repair = strategy.on_tick(_state(seconds_to_close=120, price=100_300, yes_ask=0.77, no_ask=0.25))

    assert repair.side == "long_below"
    assert repair.reason == "final-window repair smaller side only"
    assert repair.estimated_shares == pytest.approx(12)


def test_simple_inventory_final_window_does_not_seed_or_add_rich_side() -> None:
    strategy = SimpleInventoryMMStrategy(
        SimpleInventoryMMConfig(seed_contracts_per_side=50, seed_limit_price=0.58, repair_window_seconds=180)
    )

    signal = strategy.on_tick(_state(seconds_to_close=120, price=100_000, yes_ask=0.56, no_ask=0.55))

    assert signal.side == "none"
    assert signal.reason == "repair window already flat"


def test_simple_inventory_final_window_repair_respects_max_price() -> None:
    strategy = SimpleInventoryMMStrategy(SimpleInventoryMMConfig(seed_contracts_per_side=0, repair_window_seconds=180, repair_max_price=0.80))
    strategy._yes.qty = 56
    strategy._yes.avg_price = 0.56
    strategy._no.qty = 33
    strategy._no.avg_price = 0.33
    strategy._active_market_ticker = "KXBTC15M-MM"

    signal = strategy.on_tick(_state(seconds_to_close=120, price=100_300, yes_ask=0.77, no_ask=0.85))

    assert signal.side == "none"
    assert signal.reason == "repair smaller side too expensive"


def test_simple_inventory_clamps_order_size_to_remaining_net_capacity() -> None:
    strategy = SimpleInventoryMMStrategy(SimpleInventoryMMConfig(seed_contracts_per_side=0, max_net_contracts=20, cheap_add_contracts=12))
    strategy._yes.qty = 10
    strategy._yes.avg_price = 0.56
    strategy._no.qty = 0
    strategy._no.avg_price = 0.40
    strategy._active_market_ticker = "KXBTC15M-MM"

    signal = strategy.on_tick(_state(seconds_to_close=420, price=100_300, yes_ask=0.25, no_ask=0.75))

    assert signal.side == "long_above"
    assert signal.estimated_shares == pytest.approx(10)


def test_simple_inventory_next_tick_replay_never_exceeds_max_net_contracts() -> None:
    close = _contract().close_time
    ticks = [Tick(ts=close - timedelta(seconds=600 - i), price=100_000 + i, source="test") for i in range(20)]
    books = [
        OrderBookSnapshot(
            ts=tick.ts,
            market_ticker="KXBTC15M-MM",
            yes_bid=0.17,
            yes_ask=0.25,
            no_bid=0.67,
            no_ask=0.75,
        )
        for tick in ticks
    ]
    strategy = SimpleInventoryMMStrategy(
        SimpleInventoryMMConfig(seed_contracts_per_side=0, max_net_contracts=20, cheap_add_contracts=12, min_seconds_between_orders=0)
    )
    replay = ReplayEngine(
        config=BotConfig(),
        contract=_contract(),
        strategies=[strategy],
        risk_manager=RiskManager(RiskLimits(base_size_dollars=100, max_position_dollars=100, max_spread=1.0, max_open_positions=999, min_confidence=0.0)),
        executor=PaperExecutor(),
        fill_timing="next-tick",
    )

    replay.run(ticks=ticks, books=books)

    max_abs_net = max(abs(row.raw_net_contracts) for row in strategy.position_history)
    assert max_abs_net <= 20


def test_simple_inventory_next_tick_rejects_stale_passive_limit() -> None:
    close = _contract().close_time
    ticks = [
        Tick(ts=close - timedelta(seconds=600), price=100_000, source="test"),
        Tick(ts=close - timedelta(seconds=599), price=100_001, source="test"),
    ]
    books = [
        OrderBookSnapshot(ts=ticks[0].ts, market_ticker="KXBTC15M-MM", yes_bid=0.48, yes_ask=0.56, no_bid=0.47, no_ask=0.55),
        OrderBookSnapshot(ts=ticks[1].ts, market_ticker="KXBTC15M-MM", yes_bid=0.88, yes_ask=0.90, no_bid=0.07, no_ask=0.09),
    ]
    strategy = SimpleInventoryMMStrategy(SimpleInventoryMMConfig(seed_contracts_per_side=10, seed_add_contracts=10, seed_limit_price=0.58))
    replay = ReplayEngine(
        config=BotConfig(),
        contract=_contract(),
        strategies=[strategy],
        risk_manager=RiskManager(RiskLimits(base_size_dollars=100, max_position_dollars=100, max_spread=1.0, max_open_positions=10, min_confidence=0.0)),
        executor=PaperExecutor(),
        fill_timing="next-tick",
    )

    report = replay.run(ticks=ticks, books=books)

    assert report.fills == []
    assert any("passive_limit_not_touched" in result.risk.blocked_by for result in report.results)


def test_simple_inventory_next_tick_reports_passive_limit_when_other_risk_blocks() -> None:
    close = _contract().close_time
    ticks = [
        Tick(ts=close - timedelta(seconds=600), price=100_000, source="test"),
        Tick(ts=close - timedelta(seconds=599), price=100_001, source="test"),
    ]
    books = [
        OrderBookSnapshot(ts=ticks[0].ts, market_ticker="KXBTC15M-MM", yes_bid=0.48, yes_ask=0.56, no_bid=0.47, no_ask=0.55),
        OrderBookSnapshot(ts=ticks[1].ts, market_ticker="KXBTC15M-MM", yes_bid=0.82, yes_ask=0.90, no_bid=0.47, no_ask=0.55),
    ]
    strategy = SimpleInventoryMMStrategy(SimpleInventoryMMConfig(seed_contracts_per_side=10, seed_add_contracts=10, seed_limit_price=0.58))
    replay = ReplayEngine(
        config=BotConfig(),
        contract=_contract(),
        strategies=[strategy],
        risk_manager=RiskManager(RiskLimits(base_size_dollars=100, max_position_dollars=100, max_spread=1.0, max_open_positions=10, min_confidence=0.0)),
        executor=PaperExecutor(),
        fill_timing="next-tick",
    )

    report = replay.run(ticks=ticks, books=books)

    blocked_reasons = [result.risk.blocked_by for result in report.results]
    assert any("invalid_orderbook" in reasons and "passive_limit_not_touched" in reasons for reasons in blocked_reasons)


def test_simple_inventory_next_tick_discards_pending_decisions_on_market_rollover() -> None:
    close = _contract().close_time
    ticks = [
        Tick(ts=close - timedelta(seconds=600), price=100_000, source="test"),
        Tick(ts=close - timedelta(seconds=599), price=100_001, source="test"),
    ]
    books = [
        OrderBookSnapshot(ts=ticks[0].ts, market_ticker="KXBTC15M-OLD", yes_bid=0.48, yes_ask=0.56, no_bid=0.47, no_ask=0.55),
        OrderBookSnapshot(ts=ticks[1].ts, market_ticker="KXBTC15M-NEW", yes_bid=0.48, yes_ask=0.56, no_bid=0.47, no_ask=0.55),
    ]
    strategy = SimpleInventoryMMStrategy(SimpleInventoryMMConfig(seed_contracts_per_side=10, seed_add_contracts=10, seed_limit_price=0.58))
    replay = ReplayEngine(
        config=BotConfig(),
        contract=ContractWindow(ticker="KXBTC15M-OLD", strike=100_000.0, open_time=_contract().open_time, close_time=close),
        strategies=[strategy],
        risk_manager=RiskManager(RiskLimits(base_size_dollars=100, max_position_dollars=100, max_spread=1.0, max_open_positions=10, min_confidence=0.0)),
        executor=PaperExecutor(),
        fill_timing="next-tick",
    )

    report = replay.run(ticks=ticks, books=books)

    assert report.fills == []
