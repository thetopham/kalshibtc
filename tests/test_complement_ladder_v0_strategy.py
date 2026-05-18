from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kalshibtc.datafeed.models import OrderBookSnapshot, Tick
from kalshibtc.execution.paper import PaperExecutor
from kalshibtc.execution.risk import RiskManager
from kalshibtc.main import MarketStateBuilder
from kalshibtc.market.contract import ContractWindow
from kalshibtc.strategy.complement_ladder_v0 import ComplementLadderConfig, ComplementLadderV0Strategy
from kalshibtc.config import RiskLimits
from kalshibtc.strategy.registry import create_strategy, strategy_names


def _contract() -> ContractWindow:
    return ContractWindow(
        ticker="KXBTC15M-COMP",
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
            market_ticker="KXBTC15M-COMP",
            yes_bid=max(0.001, yes_ask - 0.02),
            yes_ask=yes_ask,
            no_bid=max(0.001, no_ask - 0.02),
            no_ask=no_ask,
        ),
        slope_30s=0.0,
    )


def _fill(strategy: ComplementLadderV0Strategy, state, signal) -> None:
    decision = RiskManager(
        RiskLimits(max_spread=1.0, min_confidence=0.0, max_position_dollars=1_000)
    ).evaluate(state, signal)
    fill = PaperExecutor().execute(state, decision)
    assert fill is not None
    strategy.on_fill(state, fill)


def test_complement_ladder_tuned_defaults_prioritize_pairs_and_tight_unpaired_risk() -> None:
    config = ComplementLadderConfig()

    assert config.target_pair_cost == pytest.approx(0.95)
    assert config.max_unpaired_contracts == pytest.approx(10)
    assert config.completion_only_seconds == pytest.approx(240)
    assert config.cheap_threshold == pytest.approx(0.15)
    assert config.cheap_add_contracts < config.direct_pair_contracts


def test_complement_ladder_opens_cheap_lot_with_required_opposite_price() -> None:
    strategy = ComplementLadderV0Strategy(
        ComplementLadderConfig(cheap_threshold=0.25, cheap_add_contracts=5, target_pair_cost=0.95)
    )
    state = _state(seconds_to_close=420, yes_ask=0.20, no_ask=0.82)

    signal = strategy.on_tick(state)

    assert signal.side == "long_above"
    assert signal.reason == "open cheap complement lot with target completion limit"
    assert signal.estimated_shares == pytest.approx(5)
    assert signal.features is not None
    assert signal.features["required_opposite_price"] == pytest.approx(0.75)


def test_complement_ladder_completes_lot_only_below_target_pair_cost() -> None:
    strategy = ComplementLadderV0Strategy(
        ComplementLadderConfig(cheap_threshold=0.25, cheap_add_contracts=5, target_pair_cost=0.95)
    )
    first = _state(seconds_to_close=420, yes_ask=0.20, no_ask=0.82)
    cheap = strategy.on_tick(first)
    _fill(strategy, first, cheap)

    too_expensive = strategy.on_tick(_state(seconds_to_close=390, yes_ask=0.22, no_ask=0.76))
    assert too_expensive.side == "long_above"
    assert too_expensive.reason == "open cheap complement lot with target completion limit"

    completion_state = _state(seconds_to_close=360, yes_ask=0.23, no_ask=0.75)
    completion = strategy.on_tick(completion_state)
    assert completion.side == "long_below"
    assert completion.reason == "complete open complement lot below target pair cost"
    assert completion.features is not None
    assert completion.features["projected_pair_cost"] == pytest.approx(0.95)
    _fill(strategy, completion_state, completion)

    assert strategy.paired_avg_cost == pytest.approx(0.95)
    assert strategy.locked_edge_per_pair == pytest.approx(0.05)


def test_complement_ladder_rejects_new_unpaired_lots_in_completion_window() -> None:
    strategy = ComplementLadderV0Strategy(
        ComplementLadderConfig(cheap_threshold=0.25, completion_only_seconds=60, no_trade_seconds=15)
    )

    signal = strategy.on_tick(_state(seconds_to_close=45, yes_ask=0.10, no_ask=0.88))

    assert signal.side == "none"
    assert signal.reason == "completion-only window; no new unpaired lots"


def test_complement_ladder_respects_unpaired_cap() -> None:
    strategy = ComplementLadderV0Strategy(
        ComplementLadderConfig(cheap_threshold=0.25, cheap_add_contracts=5, max_unpaired_contracts=5)
    )
    first = _state(seconds_to_close=420, yes_ask=0.20, no_ask=0.82)
    cheap = strategy.on_tick(first)
    _fill(strategy, first, cheap)

    blocked = strategy.on_tick(_state(seconds_to_close=410, yes_ask=0.19, no_ask=0.83))

    assert blocked.side == "none"
    assert blocked.reason == "max unpaired exposure reached"


def test_complement_ladder_takes_direct_pair_below_target() -> None:
    strategy = ComplementLadderV0Strategy(
        ComplementLadderConfig(target_pair_cost=0.95, direct_pair_contracts=7)
    )
    yes_state = _state(seconds_to_close=420, yes_ask=0.44, no_ask=0.50)
    yes = strategy.on_tick(yes_state)
    assert yes.side == "long_above"
    assert yes.reason == "direct complement pair below target cost"
    _fill(strategy, yes_state, yes)

    no_state = _state(seconds_to_close=419, yes_ask=0.44, no_ask=0.50)
    no = strategy.on_tick(no_state)
    assert no.side == "long_below"
    _fill(strategy, no_state, no)

    assert strategy.paired_avg_cost == pytest.approx(0.94)
    assert strategy.locked_edge_per_pair == pytest.approx(0.06)


def test_complement_ladder_is_registered_and_accepts_params() -> None:
    assert "complement_ladder_v0" in strategy_names()
    strategy = create_strategy("complement_ladder_v0", {"target_pair_cost": 0.95, "cheap_threshold": 0.2})
    assert isinstance(strategy, ComplementLadderV0Strategy)
    assert strategy.config.target_pair_cost == pytest.approx(0.95)
    assert strategy.config.cheap_threshold == pytest.approx(0.2)
