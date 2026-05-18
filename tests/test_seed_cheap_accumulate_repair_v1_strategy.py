from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kalshibtc.config import RiskLimits
from kalshibtc.datafeed.models import OrderBookSnapshot, Tick
from kalshibtc.execution.paper import PaperExecutor
from kalshibtc.execution.risk import RiskManager
from kalshibtc.main import MarketStateBuilder
from kalshibtc.market.contract import ContractWindow
from kalshibtc.strategy.registry import create_strategy, strategy_names
from kalshibtc.strategy.seed_cheap_accumulate_repair_v1 import (
    SeedCheapAccumulateRepairConfig,
    SeedCheapAccumulateRepairV1Strategy,
)


def _contract() -> ContractWindow:
    return ContractWindow(
        ticker="KXBTC15M-SEED",
        strike=100_000.0,
        open_time=datetime(2026, 5, 15, 12, 0, tzinfo=UTC),
        close_time=datetime(2026, 5, 15, 12, 15, tzinfo=UTC),
    )


def _state(*, seconds_to_close: int, price: float = 100_050.0, yes_ask: float = 0.52, no_ask: float = 0.56, slope_30s: float = 0.0):
    ts = _contract().close_time - timedelta(seconds=seconds_to_close)
    return MarketStateBuilder(contract=_contract()).from_tick_and_book(
        tick=Tick(ts=ts, price=price, source="test"),
        orderbook=OrderBookSnapshot(
            ts=ts,
            market_ticker="KXBTC15M-SEED",
            yes_bid=max(0.001, min(yes_ask - 0.01, 1.0 - no_ask - 0.01)),
            yes_ask=yes_ask,
            no_bid=max(0.001, min(no_ask - 0.01, 1.0 - yes_ask - 0.01)),
            no_ask=no_ask,
        ),
        slope_30s=slope_30s,
    )


def _fill(strategy: SeedCheapAccumulateRepairV1Strategy, state, signal) -> None:
    decision = RiskManager(
        RiskLimits(max_spread=1.0, min_confidence=0.0, max_position_dollars=1_000)
    ).evaluate(state, signal)
    fill = PaperExecutor().execute(state, decision)
    assert fill is not None
    strategy.on_fill(state, fill)


def test_seed_v1_opens_primary_then_hedge_in_3_to_1_dollar_ratio() -> None:
    strategy = SeedCheapAccumulateRepairV1Strategy(
        SeedCheapAccumulateRepairConfig(seed_primary_spend=30, seed_hedge_spend=10, max_total_cost=50)
    )
    state = _state(seconds_to_close=780, price=100_050, yes_ask=0.52, no_ask=0.56)

    primary = strategy.on_tick(state)
    assert primary.side == "long_above"
    assert primary.reason == "opening seed primary 3x"
    assert primary.target_notional == pytest.approx(30)
    _fill(strategy, state, primary)

    hedge = strategy.on_tick(_state(seconds_to_close=779, price=100_050, yes_ask=0.52, no_ask=0.56))
    assert hedge.side == "long_below"
    assert hedge.reason == "opening seed hedge 1x"
    assert hedge.target_notional == pytest.approx(10)


def test_seed_v1_chooses_no_primary_when_btc_below_strike() -> None:
    strategy = SeedCheapAccumulateRepairV1Strategy()

    signal = strategy.on_tick(_state(seconds_to_close=780, price=99_950, yes_ask=0.48, no_ask=0.54))

    assert signal.side == "long_below"
    assert signal.reason == "opening seed primary 3x"


def test_seed_v1_uses_slope_when_price_equals_strike() -> None:
    strategy = SeedCheapAccumulateRepairV1Strategy()

    signal = strategy.on_tick(_state(seconds_to_close=780, price=100_000, yes_ask=0.50, no_ask=0.50, slope_30s=-2.0))

    assert signal.side == "long_below"


def test_seed_v1_does_not_seed_outside_opening_window() -> None:
    strategy = SeedCheapAccumulateRepairV1Strategy()

    signal = strategy.on_tick(_state(seconds_to_close=600, yes_ask=0.52, no_ask=0.56))

    assert signal.side == "none"
    assert signal.reason == "no cheap side"


def test_seed_v1_cheap_accumulates_after_seed_complete() -> None:
    strategy = SeedCheapAccumulateRepairV1Strategy(
        SeedCheapAccumulateRepairConfig(cheap_price=0.15, normal_spend=1, max_total_cost=50, max_net_ratio=1.0)
    )
    strategy._active_market_ticker = "KXBTC15M-SEED"
    strategy._seed_primary_side = "yes"
    strategy._seed_primary_filled = True
    strategy._seed_hedge_filled = True
    strategy._yes.cost = 30
    strategy._yes.qty = 60
    strategy._no.cost = 10
    strategy._no.qty = 18

    signal = strategy.on_tick(_state(seconds_to_close=600, yes_ask=0.12, no_ask=0.89))

    assert signal.side == "long_above"
    assert signal.reason == "cheap side accumulation"
    assert signal.target_notional == pytest.approx(1)


def test_seed_v1_repairs_smaller_side_near_expiry() -> None:
    strategy = SeedCheapAccumulateRepairV1Strategy(
        SeedCheapAccumulateRepairConfig(repair_start_seconds=240, repair_max_price=0.85, target_net_ratio=0.05)
    )
    strategy._active_market_ticker = "KXBTC15M-SEED"
    strategy._yes.qty = 100
    strategy._yes.cost = 30
    strategy._no.qty = 40
    strategy._no.cost = 10

    signal = strategy.on_tick(_state(seconds_to_close=120, yes_ask=0.20, no_ask=0.80))

    assert signal.side == "long_below"
    assert signal.reason == "repair smaller side near expiry"
    assert signal.estimated_shares == pytest.approx(60)


def test_seed_v1_respects_total_cost_cap_across_seed() -> None:
    strategy = SeedCheapAccumulateRepairV1Strategy(
        SeedCheapAccumulateRepairConfig(seed_primary_spend=30, seed_hedge_spend=10, max_total_cost=35)
    )
    state = _state(seconds_to_close=780, yes_ask=0.50, no_ask=0.55)
    primary = strategy.on_tick(state)
    _fill(strategy, state, primary)

    hedge = strategy.on_tick(_state(seconds_to_close=779, yes_ask=0.50, no_ask=0.55))

    assert hedge.side == "long_below"
    assert hedge.target_notional == pytest.approx(5)


def test_seed_v1_blocks_dominant_side_when_max_net_ratio_exceeded() -> None:
    strategy = SeedCheapAccumulateRepairV1Strategy(
        SeedCheapAccumulateRepairConfig(cheap_price=0.15, normal_spend=1, max_net_ratio=0.35)
    )
    strategy._active_market_ticker = "KXBTC15M-SEED"
    strategy._seed_primary_side = "yes"
    strategy._seed_primary_filled = True
    strategy._seed_hedge_filled = True
    strategy._yes.qty = 100
    strategy._yes.cost = 30
    strategy._no.qty = 20
    strategy._no.cost = 10

    signal = strategy.on_tick(_state(seconds_to_close=600, yes_ask=0.12, no_ask=0.89))

    assert signal.side == "none"
    assert signal.reason == "dominant side blocked by max net ratio"


def test_seed_v1_allows_non_dominant_side_when_max_net_ratio_exceeded() -> None:
    strategy = SeedCheapAccumulateRepairV1Strategy(
        SeedCheapAccumulateRepairConfig(cheap_price=0.15, normal_spend=1, max_net_ratio=0.35)
    )
    strategy._active_market_ticker = "KXBTC15M-SEED"
    strategy._seed_primary_side = "yes"
    strategy._seed_primary_filled = True
    strategy._seed_hedge_filled = True
    strategy._yes.qty = 100
    strategy._yes.cost = 30
    strategy._no.qty = 20
    strategy._no.cost = 10

    signal = strategy.on_tick(_state(seconds_to_close=600, yes_ask=0.89, no_ask=0.12))

    assert signal.side == "long_below"
    assert signal.reason == "cheap side accumulation"


def test_seed_v1_blocks_orders_below_min_contract_size() -> None:
    strategy = SeedCheapAccumulateRepairV1Strategy(
        SeedCheapAccumulateRepairConfig(cheap_price=0.15, normal_spend=0.25, min_order_contracts=5)
    )
    strategy._active_market_ticker = "KXBTC15M-SEED"
    strategy._seed_primary_side = "yes"
    strategy._seed_primary_filled = True
    strategy._seed_hedge_filled = True

    signal = strategy.on_tick(_state(seconds_to_close=600, yes_ask=0.10, no_ask=0.89))

    assert signal.side == "none"
    assert signal.reason == "below min order contracts"


def test_seed_v1_does_not_reemit_same_resting_price_level_before_fill() -> None:
    strategy = SeedCheapAccumulateRepairV1Strategy(
        SeedCheapAccumulateRepairConfig(cheap_price=0.15, normal_spend=1, max_net_ratio=1.0, one_fill_per_price_level=True)
    )
    strategy._active_market_ticker = "KXBTC15M-SEED"
    strategy._seed_primary_side = "yes"
    strategy._seed_primary_filled = True
    strategy._seed_hedge_filled = True

    first = strategy.on_tick(_state(seconds_to_close=600, yes_ask=0.10, no_ask=0.89))
    second = strategy.on_tick(_state(seconds_to_close=599, yes_ask=0.10, no_ask=0.89))

    assert first.side == "long_above"
    assert second.side == "none"
    assert second.reason == "resting limit order already active"


def test_seed_v1_does_not_refill_same_price_level_after_fill() -> None:
    strategy = SeedCheapAccumulateRepairV1Strategy(
        SeedCheapAccumulateRepairConfig(cheap_price=0.15, normal_spend=1, max_net_ratio=1.0, one_fill_per_price_level=True)
    )
    strategy._active_market_ticker = "KXBTC15M-SEED"
    strategy._seed_primary_side = "yes"
    strategy._seed_primary_filled = True
    strategy._seed_hedge_filled = True
    state = _state(seconds_to_close=600, yes_ask=0.10, no_ask=0.89)

    first = strategy.on_tick(state)
    _fill(strategy, state, first)
    second = strategy.on_tick(_state(seconds_to_close=599, yes_ask=0.10, no_ask=0.89))

    assert first.side == "long_above"
    assert second.side == "none"
    assert second.reason == "resting limit price level already filled"


def test_seed_v1_registered_and_accepts_params() -> None:
    assert "seed_cheap_accumulate_repair_v1" in strategy_names()
    strategy = create_strategy("seed_cheap_accumulate_repair_v1", {"seed_primary_spend": 12, "repair_start_seconds": 480})
    assert isinstance(strategy, SeedCheapAccumulateRepairV1Strategy)
    assert strategy.config.seed_primary_spend == pytest.approx(12)
    assert strategy.config.repair_start_seconds == pytest.approx(480)
