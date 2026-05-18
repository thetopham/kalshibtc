from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kalshibtc.config import RiskLimits
from kalshibtc.datafeed.models import OrderBookSnapshot, Tick
from kalshibtc.execution.paper import PaperExecutor
from kalshibtc.execution.risk import RiskManager
from kalshibtc.main import MarketStateBuilder
from kalshibtc.market.contract import ContractWindow
from kalshibtc.strategy.cheap_accumulate_repair_v0 import CheapAccumulateRepairConfig, CheapAccumulateRepairV0Strategy
from kalshibtc.strategy.registry import create_strategy, strategy_names


def _contract() -> ContractWindow:
    return ContractWindow(
        ticker="KXBTC15M-CHEAP",
        strike=100_000.0,
        open_time=datetime(2026, 5, 15, 12, 0, tzinfo=UTC),
        close_time=datetime(2026, 5, 15, 12, 15, tzinfo=UTC),
    )


def _state(*, seconds_to_close: int, price: float = 100_050.0, yes_ask: float, no_ask: float):
    ts = _contract().close_time - timedelta(seconds=seconds_to_close)
    return MarketStateBuilder(contract=_contract()).from_tick_and_book(
        tick=Tick(ts=ts, price=price, source="test"),
        orderbook=OrderBookSnapshot(
            ts=ts,
            market_ticker="KXBTC15M-CHEAP",
            yes_bid=max(0.001, yes_ask - 0.02),
            yes_ask=yes_ask,
            no_bid=max(0.001, no_ask - 0.02),
            no_ask=no_ask,
        ),
        slope_30s=0.0,
    )


def _fill(strategy: CheapAccumulateRepairV0Strategy, state, signal) -> None:
    decision = RiskManager(
        RiskLimits(max_spread=1.0, min_confidence=0.0, max_position_dollars=1_000)
    ).evaluate(state, signal)
    fill = PaperExecutor().execute(state, decision)
    assert fill is not None
    strategy.on_fill(state, fill)


def test_cheap_accumulate_buys_cheapest_side_before_repair_window() -> None:
    strategy = CheapAccumulateRepairV0Strategy(
        CheapAccumulateRepairConfig(cheap_price=0.25, normal_spend=10, max_total_cost=200)
    )

    signal = strategy.on_tick(_state(seconds_to_close=420, yes_ask=0.20, no_ask=0.82))

    assert signal.side == "long_above"
    assert signal.reason == "cheap side accumulation"
    assert signal.target_notional == pytest.approx(10)
    assert signal.estimated_shares == pytest.approx(50)


def test_cheap_accumulate_uses_bigger_spend_for_very_cheap_side() -> None:
    strategy = CheapAccumulateRepairV0Strategy(
        CheapAccumulateRepairConfig(very_cheap_price=0.10, very_cheap_spend=25)
    )

    signal = strategy.on_tick(_state(seconds_to_close=420, yes_ask=0.08, no_ask=0.90))

    assert signal.side == "long_above"
    assert signal.reason == "very cheap side accumulation"
    assert signal.target_notional == pytest.approx(25)


def test_cheap_accumulate_respects_total_cost_cap() -> None:
    strategy = CheapAccumulateRepairV0Strategy(
        CheapAccumulateRepairConfig(cheap_price=0.25, normal_spend=10, max_total_cost=10)
    )
    state = _state(seconds_to_close=420, yes_ask=0.20, no_ask=0.82)
    first = strategy.on_tick(state)
    _fill(strategy, state, first)

    blocked = strategy.on_tick(_state(seconds_to_close=419, yes_ask=0.20, no_ask=0.82))

    assert blocked.side == "none"
    assert blocked.reason == "max total cost reached"


def test_cheap_accumulate_repairs_smaller_side_near_expiry() -> None:
    strategy = CheapAccumulateRepairV0Strategy(
        CheapAccumulateRepairConfig(repair_start_seconds=120, repair_max_price=0.85, target_net_ratio=0.05)
    )
    strategy._active_market_ticker = "KXBTC15M-CHEAP"
    strategy._yes.qty = 100
    strategy._yes.cost = 20
    strategy._no.qty = 40
    strategy._no.cost = 30

    repair = strategy.on_tick(_state(seconds_to_close=90, yes_ask=0.20, no_ask=0.80))

    assert repair.side == "long_below"
    assert repair.reason == "repair smaller side near expiry"
    assert repair.estimated_shares == pytest.approx(60)


def test_cheap_accumulate_does_not_repair_when_net_ratio_target_reached() -> None:
    strategy = CheapAccumulateRepairV0Strategy(CheapAccumulateRepairConfig(target_net_ratio=0.05))
    strategy._active_market_ticker = "KXBTC15M-CHEAP"
    strategy._yes.qty = 100
    strategy._yes.cost = 20
    strategy._no.qty = 97
    strategy._no.cost = 70

    signal = strategy.on_tick(_state(seconds_to_close=90, yes_ask=0.20, no_ask=0.80))

    assert signal.side == "none"
    assert signal.reason == "repair target net ratio reached"


def test_cheap_accumulate_registered_and_accepts_params() -> None:
    assert "cheap_accumulate_repair_v0" in strategy_names()
    strategy = create_strategy("cheap_accumulate_repair_v0", {"cheap_price": 0.2, "max_total_cost": 123})
    assert isinstance(strategy, CheapAccumulateRepairV0Strategy)
    assert strategy.config.cheap_price == pytest.approx(0.2)
    assert strategy.config.max_total_cost == pytest.approx(123)
