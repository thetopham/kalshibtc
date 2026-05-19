from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kalshibtc.config import RiskLimits
from kalshibtc.datafeed.models import OrderBookSnapshot, Tick
from kalshibtc.execution.paper import PaperExecutor
from kalshibtc.execution.risk import RiskManager
from kalshibtc.main import MarketStateBuilder
from kalshibtc.market.contract import ContractWindow
from kalshibtc.strategy.inventory_aware_passive_mm import (
    InventoryAwarePassiveMMConfig,
    InventoryAwarePassiveMMStrategy,
)
from kalshibtc.strategy.registry import create_strategy, strategy_names


def _contract() -> ContractWindow:
    return ContractWindow(
        ticker="POLY-BTC15M-IAPMM",
        strike=100_000.0,
        open_time=datetime(2026, 5, 18, 12, 0, tzinfo=UTC),
        close_time=datetime(2026, 5, 18, 12, 15, tzinfo=UTC),
    )


def _state(
    *,
    seconds_after_open: int | None = None,
    seconds_to_close: int | None = None,
    price: float = 100_050.0,
    yes_bid: float = 0.50,
    yes_ask: float = 0.54,
    no_bid: float = 0.44,
    no_ask: float = 0.48,
    raw: dict | None = None,
):
    contract = _contract()
    assert contract.open_time is not None
    if seconds_after_open is not None:
        ts = contract.open_time + timedelta(seconds=seconds_after_open)
    elif seconds_to_close is not None:
        ts = contract.close_time - timedelta(seconds=seconds_to_close)
    else:
        ts = contract.open_time + timedelta(seconds=30)
    return MarketStateBuilder(contract=contract).from_tick_and_book(
        tick=Tick(ts=ts, price=price, source="test", raw=raw or {}),
        orderbook=OrderBookSnapshot(
            ts=ts,
            market_ticker=contract.ticker,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            raw=raw or {},
        ),
        slope_30s=0.0,
    )


def _fill(strategy: InventoryAwarePassiveMMStrategy, state, signal) -> None:
    decision = RiskManager(
        RiskLimits(max_spread=1.0, min_confidence=0.0, max_position_dollars=10_000, max_open_positions=999)
    ).evaluate(state, signal)
    fill = PaperExecutor().execute(state, decision)
    assert fill is not None
    strategy.on_fill(state, fill)


def test_inventory_aware_passive_mm_registered_and_accepts_params() -> None:
    assert "inventory_aware_passive_mm" in strategy_names()
    strategy = create_strategy("inventory_aware_passive_mm", {"target_pair_cost": 0.91, "min_visible_depth": 0})
    assert isinstance(strategy, InventoryAwarePassiveMMStrategy)
    assert strategy.config.target_pair_cost == pytest.approx(0.91)


def test_open_ladder_emits_passive_yes_quote_with_pair_metadata() -> None:
    strategy = InventoryAwarePassiveMMStrategy(
        InventoryAwarePassiveMMConfig(min_visible_depth=0, open_ladder_levels=((0.05, 25),))
    )

    signal = strategy.on_tick(_state(seconds_after_open=30, yes_bid=0.49, yes_ask=0.55, no_bid=0.43, no_ask=0.49))

    assert signal.side == "long_above"
    assert signal.reason == "opening passive ladder"
    assert signal.features is not None
    assert signal.features["limit_price"] == pytest.approx(0.47)
    assert signal.features["projected_unpaired_side"] == "yes"
    assert signal.features["probability_yes"] == pytest.approx(0.5193144628)
    assert signal.features["probability_no"] == pytest.approx(0.4806855372)
    assert signal.features["yes_model_edge_vs_mid"] == pytest.approx(-0.0006855372)
    assert signal.features["no_model_edge_vs_mid"] == pytest.approx(0.0206855372)
    assert signal.features["edge_yes"] == pytest.approx(-0.0306855372)
    assert signal.features["edge_no"] == pytest.approx(-0.0093144628)
    assert signal.features["yes_model_edge_vs_ask"] == signal.features["edge_yes"]
    assert signal.features["no_model_edge_vs_ask"] == signal.features["edge_no"]
    assert signal.allow_price_strike_mismatch is True


def test_weak_side_repair_allowed_under_hard_ceiling_and_weak_not_above_strong() -> None:
    strategy = InventoryAwarePassiveMMStrategy(
        InventoryAwarePassiveMMConfig(
            min_visible_depth=0,
            target_pair_cost=0.95,
            hard_pair_cost_ceiling=0.99,
            max_unpaired_strong_qty=300,
            max_unpaired_weak_qty=0,
            repair_ladder_levels=((0.30, 200),),
        )
    )
    strategy._active_market_ticker = _contract().ticker
    strategy._inventory.apply_buy(side="yes", price=0.60, qty=100)

    signal = strategy.on_tick(
        _state(seconds_to_close=150, price=100_200, yes_bid=0.72, yes_ask=0.76, no_bid=0.18, no_ask=0.24)
    )

    assert signal.side == "long_below"
    assert signal.reason == "inventory repair ladder"
    assert signal.features is not None
    assert signal.features["projected_pair_cost"] == pytest.approx(0.90)
    assert signal.features["projected_unpaired_side"] is None


def test_weak_side_repair_blocked_when_it_would_exceed_strong_side() -> None:
    strategy = InventoryAwarePassiveMMStrategy(
        InventoryAwarePassiveMMConfig(
            min_visible_depth=0,
            max_weak_to_strong_ratio=0.5,
            repair_ladder_levels=((0.30, 200),),
        )
    )
    strategy._active_market_ticker = _contract().ticker
    strategy._inventory.apply_buy(side="yes", price=0.60, qty=50)

    signal = strategy.on_tick(
        _state(seconds_to_close=150, price=100_200, yes_bid=0.72, yes_ask=0.76, no_bid=0.18, no_ask=0.24)
    )

    assert signal.side == "none"
    assert signal.reason == "weak_exceeds_strong"


def test_pair_cost_above_absolute_ceiling_blocks_candidate() -> None:
    strategy = InventoryAwarePassiveMMStrategy(
        InventoryAwarePassiveMMConfig(
            min_visible_depth=0,
            target_pair_cost=0.95,
            hard_pair_cost_ceiling=0.99,
            repair_ladder_levels=((0.50, 50),),
        )
    )
    strategy._active_market_ticker = _contract().ticker
    strategy._inventory.apply_buy(side="yes", price=0.70, qty=50)

    signal = strategy.on_tick(
        _state(seconds_to_close=150, price=100_200, yes_bid=0.62, yes_ask=0.70, no_bid=0.40, no_ask=0.50)
    )

    assert signal.side == "none"
    assert signal.reason == "pair_cost_above_absolute_ceiling"


def test_visible_depth_requirement_blocks_thin_touched_books() -> None:
    strategy = InventoryAwarePassiveMMStrategy(
        InventoryAwarePassiveMMConfig(min_visible_depth=25, open_ladder_levels=((0.03, 25),))
    )
    raw = {"yes_orderbook": {"asks": [{"price": "0.47", "size": "10"}]}}

    signal = strategy.on_tick(_state(seconds_after_open=30, yes_bid=0.49, yes_ask=0.51, no_bid=0.43, no_ask=0.49, raw=raw))

    assert signal.side == "none"
    assert signal.reason == "insufficient_depth"


def test_untouched_passive_quote_is_not_blocked_by_zero_current_depth() -> None:
    strategy = InventoryAwarePassiveMMStrategy(
        InventoryAwarePassiveMMConfig(min_visible_depth=25, open_ladder_levels=((0.05, 25),))
    )
    raw = {"yes_orderbook": {"asks": [{"price": "0.55", "size": "10"}]}}

    signal = strategy.on_tick(_state(seconds_after_open=30, yes_bid=0.49, yes_ask=0.55, no_bid=0.43, no_ask=0.49, raw=raw))

    assert signal.side == "long_above"
    assert signal.reason == "opening passive ladder"

def test_on_fill_updates_inventory_and_matched_pair_edge() -> None:
    strategy = InventoryAwarePassiveMMStrategy(
        InventoryAwarePassiveMMConfig(min_visible_depth=0, open_ladder_levels=((0.05, 25),))
    )
    state = _state(seconds_after_open=30, yes_bid=0.49, yes_ask=0.47, no_bid=0.43, no_ask=0.49)
    signal = strategy.on_tick(state)
    _fill(strategy, state, signal)
    strategy._inventory.apply_buy(side="no", price=0.40, qty=25)

    snapshot = strategy._inventory.to_snapshot()
    assert snapshot["matched_pair_cost"] == pytest.approx(0.87)
    matched_qty = snapshot["matched_qty"]
    assert isinstance(matched_qty, float)
    assert snapshot["locked_edge_if_held"] == pytest.approx(matched_qty * (1 - 0.87))
