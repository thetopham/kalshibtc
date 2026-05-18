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
from kalshibtc.strategy.seed_cheap_accumulate_repair_v2 import (
    SeedCheapAccumulateRepairV2Config,
    SeedCheapAccumulateRepairV2Strategy,
)


def _contract() -> ContractWindow:
    return ContractWindow(
        ticker="KXBTC15M-SEED-V2",
        strike=100_000.0,
        open_time=datetime(2026, 5, 15, 12, 0, tzinfo=UTC),
        close_time=datetime(2026, 5, 15, 12, 15, tzinfo=UTC),
    )


def _state(*, seconds_to_close: int, price: float = 100_050.0, yes_ask: float = 0.52, no_ask: float = 0.56, slope_30s: float = 0.0, raw: dict | None = None):
    ts = _contract().close_time - timedelta(seconds=seconds_to_close)
    return MarketStateBuilder(contract=_contract()).from_tick_and_book(
        tick=Tick(ts=ts, price=price, source="test"),
        orderbook=OrderBookSnapshot(
            ts=ts,
            market_ticker="KXBTC15M-SEED-V2",
            yes_bid=max(0.001, min(yes_ask - 0.10, 1.0 - no_ask - 0.01)),
            yes_ask=yes_ask,
            no_bid=max(0.001, min(no_ask - 0.01, 1.0 - yes_ask - 0.01)),
            no_ask=no_ask,
            raw=raw or {},
        ),
        slope_30s=slope_30s,
    )


def _fill(strategy: SeedCheapAccumulateRepairV2Strategy, state, signal) -> None:
    decision = RiskManager(
        RiskLimits(max_spread=1.0, min_confidence=0.0, max_position_dollars=1_000)
    ).evaluate(state, signal)
    fill = PaperExecutor().execute(state, decision)
    assert fill is not None
    strategy.on_fill(state, fill)


def _seeded_strategy(**params) -> SeedCheapAccumulateRepairV2Strategy:
    strategy = SeedCheapAccumulateRepairV2Strategy(SeedCheapAccumulateRepairV2Config(**params))
    strategy._active_market_ticker = "KXBTC15M-SEED-V2"
    strategy._seed_primary_side = "yes"
    strategy._seed_primary_filled = True
    strategy._seed_hedge_filled = True
    return strategy


def test_seed_v2_blocks_dominant_side_add_even_when_cheap() -> None:
    strategy = _seeded_strategy(cheap_price=0.15, normal_spend=1, max_total_cost=100)
    strategy._yes.qty = 100
    strategy._yes.cost = 45
    strategy._no.qty = 30
    strategy._no.cost = 12

    signal = strategy.on_tick(_state(seconds_to_close=600, yes_ask=0.10, no_ask=0.89))

    assert signal.side == "none"
    assert signal.reason == "dominant side add blocked by exposure rule"


def test_seed_v2_continuously_repairs_before_late_window_when_opposite_side_is_cheap() -> None:
    strategy = _seeded_strategy(target_pair_cost=0.95, repair_max_price=0.85, normal_spend=20, max_total_cost=100)
    strategy._yes.qty = 100
    strategy._yes.cost = 50
    strategy._no.qty = 40
    strategy._no.cost = 12

    signal = strategy.on_tick(_state(seconds_to_close=600, yes_ask=0.80, no_ask=0.20))

    assert signal.side == "long_below"
    assert signal.reason == "continuous repair reduces exposure"
    assert signal.estimated_shares is not None
    assert signal.estimated_shares <= 60


def test_seed_v2_blocks_pair_building_when_average_pair_cost_exceeds_target() -> None:
    strategy = _seeded_strategy(target_pair_cost=0.95, repair_max_price=0.85, normal_spend=20, max_total_cost=100)
    strategy._yes.qty = 100
    strategy._yes.cost = 70
    strategy._no.qty = 40
    strategy._no.cost = 12

    signal = strategy.on_tick(_state(seconds_to_close=600, yes_ask=0.80, no_ask=0.40))

    assert signal.side == "none"
    assert signal.reason == "pair cost target not met"


def test_seed_v2_dynamic_net_cap_tightens_near_expiry() -> None:
    strategy = _seeded_strategy(target_pair_cost=0.95, repair_max_price=0.85, normal_spend=20, max_total_cost=100)
    strategy._yes.qty = 100
    strategy._yes.cost = 50
    strategy._no.qty = 92
    strategy._no.cost = 36

    early = strategy.on_tick(_state(seconds_to_close=600, yes_ask=0.20, no_ask=0.80))
    final = strategy.on_tick(_state(seconds_to_close=100, yes_ask=0.20, no_ask=0.80))

    assert early.side == "none"
    assert early.reason == "dynamic net cap satisfied"
    assert final.side == "long_below"
    assert final.reason == "final neutralization repair"


def test_seed_v2_final_120_seconds_blocks_new_directional_adds_without_inventory() -> None:
    strategy = _seeded_strategy(cheap_price=0.15, normal_spend=1, max_total_cost=100)

    signal = strategy.on_tick(_state(seconds_to_close=100, yes_ask=0.10, no_ask=0.89))

    assert signal.side == "none"
    assert signal.reason == "final neutralization blocks directional adds"


def test_seed_v2_balanced_seed_uses_equal_primary_and_hedge_spend() -> None:
    strategy = SeedCheapAccumulateRepairV2Strategy(
        SeedCheapAccumulateRepairV2Config(seed_mode="balanced", seed_primary_spend=30, seed_hedge_spend=30, max_total_cost=80, target_pair_cost=1.10)
    )
    state = _state(seconds_to_close=780, price=100_050, yes_ask=0.52, no_ask=0.56)

    primary = strategy.on_tick(state)
    _fill(strategy, state, primary)
    hedge = strategy.on_tick(_state(seconds_to_close=779, price=100_050, yes_ask=0.52, no_ask=0.56))

    assert primary.side == "long_above"
    assert primary.target_notional == pytest.approx(30)
    assert hedge.side == "long_below"
    assert hedge.reason == "opening balanced hedge"
    assert hedge.target_notional == pytest.approx(30)


def test_seed_v2_cheap_only_seed_waits_until_one_side_is_cheap() -> None:
    strategy = SeedCheapAccumulateRepairV2Strategy(
        SeedCheapAccumulateRepairV2Config(seed_mode="cheap_only", cheap_price=0.15, normal_spend=2, max_total_cost=20)
    )

    expensive = strategy.on_tick(_state(seconds_to_close=780, yes_ask=0.52, no_ask=0.56))
    cheap = strategy.on_tick(_state(seconds_to_close=779, yes_ask=0.14, no_ask=0.86))

    assert expensive.side == "none"
    assert expensive.reason == "cheap-only seed waiting for cheap side"
    assert cheap.side == "long_above"
    assert cheap.reason == "cheap-only seed"


def test_seed_v2_liquidity_sizing_caps_order_to_visible_depth_participation() -> None:
    strategy = SeedCheapAccumulateRepairV2Strategy(
        SeedCheapAccumulateRepairV2Config(
            seed_mode="cheap_only",
            normal_spend=20,
            max_order_notional=30,
            liquidity_participation_rate=0.20,
            min_depth_contracts=10,
            max_total_cost=100,
        )
    )
    raw = {"yes_orderbook": {"asks": [{"price": "0.14", "size": "100"}]}}

    signal = strategy.on_tick(_state(seconds_to_close=780, yes_ask=0.14, no_ask=0.86, raw=raw))

    assert signal.side == "long_above"
    assert signal.estimated_shares == pytest.approx(20)
    assert signal.target_notional == pytest.approx(2.8)
    assert signal.features["visible_depth_contracts"] == pytest.approx(100)
    assert signal.features["liquidity_participation_rate"] == pytest.approx(0.20)


def test_seed_v2_liquidity_sizing_requires_min_depth() -> None:
    strategy = SeedCheapAccumulateRepairV2Strategy(
        SeedCheapAccumulateRepairV2Config(
            seed_mode="cheap_only",
            normal_spend=20,
            liquidity_participation_rate=0.20,
            min_depth_contracts=50,
        )
    )
    raw = {"yes_orderbook": {"asks": [{"price": "0.14", "size": "40"}]}}

    signal = strategy.on_tick(_state(seconds_to_close=780, yes_ask=0.14, no_ask=0.86, raw=raw))

    assert signal.side == "none"
    assert signal.reason == "insufficient visible ask depth"


def test_seed_v2_spread_penalty_halves_size_when_spread_is_wide() -> None:
    strategy = SeedCheapAccumulateRepairV2Strategy(
        SeedCheapAccumulateRepairV2Config(
            seed_mode="cheap_only",
            normal_spend=20,
            max_order_notional=30,
            liquidity_participation_rate=1.0,
            min_depth_contracts=10,
            cheap_persistence_seconds_for_boost=9999,
            spread_penalty_enabled=True,
            wide_spread_threshold=0.05,
            spread_penalty_multiplier=0.5,
        )
    )
    raw = {"yes_orderbook": {"asks": [{"price": "0.14", "size": "100"}]}}

    signal = strategy.on_tick(_state(seconds_to_close=780, yes_ask=0.14, no_ask=0.86, raw=raw))

    assert signal.side == "long_above"
    assert signal.estimated_shares == pytest.approx(50)
    assert signal.features["spread_penalty_multiplier"] == pytest.approx(0.5)


def test_seed_v2_repair_size_multiplier_allows_larger_repair_than_fresh_entry() -> None:
    strategy = _seeded_strategy(
        target_pair_cost=0.95,
        repair_max_price=0.85,
        normal_spend=10,
        max_order_notional=30,
        repair_size_multiplier=2.0,
        liquidity_participation_rate=1.0,
        min_depth_contracts=10,
        max_total_cost=100,
    )
    strategy._yes.qty = 100
    strategy._yes.cost = 50
    strategy._no.qty = 40
    strategy._no.cost = 12
    raw = {"no_orderbook": {"asks": [{"price": "0.20", "size": "100"}]}}

    signal = strategy.on_tick(_state(seconds_to_close=600, yes_ask=0.80, no_ask=0.20, raw=raw))

    assert signal.side == "long_below"
    assert signal.estimated_shares == pytest.approx(100)
    assert signal.target_notional == pytest.approx(20)
    assert signal.features["repair_reduces_exposure"] is True


def test_seed_v2_pair_cost_constraint_clamps_liquidity_sized_repair() -> None:
    strategy = _seeded_strategy(
        target_pair_cost=0.90,
        repair_max_price=0.85,
        normal_spend=40,
        max_order_notional=100,
        liquidity_participation_rate=1.0,
        min_depth_contracts=10,
        max_total_cost=100,
    )
    strategy._yes.qty = 100
    strategy._yes.cost = 60
    strategy._no.qty = 20
    strategy._no.cost = 4
    raw = {"no_orderbook": {"asks": [{"price": "0.40", "size": "100"}]}}

    signal = strategy.on_tick(_state(seconds_to_close=600, yes_ask=0.60, no_ask=0.40, raw=raw))

    assert signal.side == "long_below"
    assert signal.estimated_shares == pytest.approx(20)
    assert signal.target_notional == pytest.approx(8)
    assert signal.features["pair_cost_allowed_contracts"] == pytest.approx(20)


def test_seed_v2_registered_and_accepts_params() -> None:
    assert "seed_cheap_accumulate_repair_v2" in strategy_names()
    strategy = create_strategy("seed_cheap_accumulate_repair_v2", {"target_pair_cost": 0.9, "seed_mode": "cheap_only"})
    assert isinstance(strategy, SeedCheapAccumulateRepairV2Strategy)
    assert strategy.config.target_pair_cost == pytest.approx(0.9)
    assert strategy.config.seed_mode == "cheap_only"
