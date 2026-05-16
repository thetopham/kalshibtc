from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kalshibtc.execution.volatility_hedge import (
    HedgeProposal,
    LifecyclePhase,
    PositionBalancer,
    PositionMode,
    VolatilityHedgeConfig,
    VolatilityHedgeFeatures,
    VolatilityHedgePosition,
    event_key_for_volatility_hedge,
)


def _ts(offset: int = 0) -> datetime:
    return datetime(2026, 1, 1, 0, 0, tzinfo=UTC) + timedelta(seconds=offset)


def _features(**overrides: float) -> VolatilityHedgeFeatures:
    values = {
        "time_to_expiry": 600.0,
        "slope": 1.0,
        "atr": 20.0,
        "atr_expansion_rate": 1.0,
        "distance_from_strike": 25.0,
        "up_bid": 0.48,
        "up_ask": 0.50,
        "down_bid": 0.48,
        "down_ask": 0.50,
    }
    values.update(overrides)
    return VolatilityHedgeFeatures(**values)


def _position(up_qty: float, down_qty: float, up_avg: float = 0.45, down_avg: float = 0.45) -> VolatilityHedgePosition:
    pos = VolatilityHedgePosition("event", "ticker", "close", 100_000)
    features = _features()
    if up_qty:
        pos.add_fill(side="UP", price=up_avg, qty=up_qty, fee=0, ts=_ts(), reason="seed", projected_paired_cost=None, features=features)
    if down_qty:
        pos.add_fill(side="DOWN", price=down_avg, qty=down_qty, fee=0, ts=_ts(), reason="seed", projected_paired_cost=up_avg + down_avg, features=features)
    return pos


def _proposal(side: str, price: float = 0.20, qty: float = 100.0, reason: str = "volatility_cheap_side_add") -> HedgeProposal:
    return HedgeProposal(side=side, price=price, qty=qty, reason=reason, confidence=0.75, features=_features())


def _manager_decision(
    *,
    manager: Any | None = None,
    seconds_to_close: float,
    up_ask: float,
    down_ask: float,
    slope: float = 1.0,
    offset: int = 0,
    event_key: str = "event",
    market_ticker: str = "ticker",
    market_close_time: str = "close",
    strike: float = 100_000,
):
    from kalshibtc.execution.volatility_hedge import VolatilityHedgeManager

    mgr = manager or VolatilityHedgeManager(VolatilityHedgeConfig(slippage=0, cooldown_seconds=0))
    decision = mgr.on_book(
        event_key=event_key,
        market_ticker=market_ticker,
        ts=_ts(offset),
        market_close_time=market_close_time,
        strike=strike,
        seconds_to_close=seconds_to_close,
        distance_from_strike=20,
        btc_price=100_020,
        slope=slope,
        up_bid=max(0.01, up_ask - 0.01),
        up_ask=up_ask,
        down_bid=max(0.01, down_ask - 0.01),
        down_ask=down_ask,
        atr=10,
    )
    return mgr, decision


def test_balanced_inventory_allows_normal_paired_cost_improving_add() -> None:
    balancer = PositionBalancer(VolatilityHedgeConfig(min_projected_pair_cost_improvement=0.001))
    pos = _position(100, 100, up_avg=0.55, down_avg=0.45)

    decision = balancer.evaluate(pos, _proposal("DOWN", price=0.20, qty=50))

    assert decision.allowed is True
    assert decision.mode == PositionMode.NORMAL
    assert decision.final_qty == 50
    assert decision.projected_paired_cost < decision.current_paired_cost
    assert decision.projected_settlement_ev >= decision.current_settlement_ev - 1e-9


def test_three_to_two_inventory_is_acceptable_and_not_repair() -> None:
    balancer = PositionBalancer(VolatilityHedgeConfig())
    pos = _position(150, 100)

    decision = balancer.evaluate(pos, _proposal("DOWN", price=0.30, qty=10))

    assert decision.current_imbalance_ratio == pytest.approx(1.5)
    assert decision.mode == PositionMode.NORMAL


def test_more_than_two_to_one_inventory_triggers_repair_mode_and_repair_qty_math() -> None:
    balancer = PositionBalancer(VolatilityHedgeConfig(target_lean_ratio=1.5, repair_imbalance_ratio=2.0))
    pos = _position(900, 300)

    decision = balancer.evaluate(pos, _proposal("DOWN", price=0.20, qty=999))

    assert decision.mode == PositionMode.REPAIR
    assert decision.repair_qty_needed == pytest.approx(300)
    assert decision.final_qty <= 100
    assert decision.projected_imbalance_ratio < decision.current_imbalance_ratio


def test_repair_mode_blocks_larger_side_add() -> None:
    balancer = PositionBalancer(VolatilityHedgeConfig(repair_imbalance_ratio=2.0))
    pos = _position(900, 300)

    decision = balancer.evaluate(pos, _proposal("UP", price=0.05, qty=50))

    assert decision.allowed is False
    assert decision.mode == PositionMode.REPAIR
    assert decision.reason == "repair_mode_blocks_larger_side"


def test_hard_and_emergency_imbalance_block_normal_larger_side_adds() -> None:
    balancer = PositionBalancer(VolatilityHedgeConfig(hard_imbalance_ratio=3.0, emergency_imbalance_ratio=4.0))
    hard = _position(350, 100)
    emergency = _position(500, 100)

    hard_decision = balancer.evaluate(hard, _proposal("UP", price=0.05, qty=10))
    emergency_decision = balancer.evaluate(emergency, _proposal("UP", price=0.05, qty=10))

    assert hard_decision.allowed is False
    assert hard_decision.mode == PositionMode.REPAIR
    assert hard_decision.reason == "repair_mode_blocks_larger_side"
    assert emergency_decision.allowed is False
    assert emergency_decision.mode == PositionMode.EMERGENCY
    assert emergency_decision.reason == "repair_mode_blocks_larger_side"


def test_notional_and_side_caps_block_oversized_positions() -> None:
    balancer = PositionBalancer(VolatilityHedgeConfig(max_notional_per_market=100, max_notional_per_side=60, base_add_notional=100, max_contracts_per_add=200))
    pos = _position(100, 100, up_avg=0.30, down_avg=0.30)  # $60 total, $30 per side

    market_block = balancer.evaluate(pos, _proposal("DOWN", price=0.50, qty=100))
    side_block = balancer.evaluate(pos, _proposal("DOWN", price=0.50, qty=70))

    assert market_block.allowed is False
    assert market_block.reason == "max_market_notional"
    assert side_block.allowed is False
    assert side_block.reason == "max_side_notional"


def test_settlement_ev_and_worst_case_pnl_calculation() -> None:
    balancer = PositionBalancer(VolatilityHedgeConfig())
    pos = _position(100, 50, up_avg=0.40, down_avg=0.20)
    features = _features(up_bid=0.59, up_ask=0.61, down_bid=0.39, down_ask=0.41)

    metrics = balancer.metrics(pos, features)

    assert metrics.p_up == pytest.approx(0.60)
    assert metrics.total_cost == pytest.approx(50.0)
    assert metrics.expected_settlement_value == pytest.approx(80.0)
    assert metrics.settlement_ev == pytest.approx(30.0)
    assert metrics.up_win_pnl == pytest.approx(50.0)
    assert metrics.down_win_pnl == pytest.approx(0.0)
    assert metrics.worst_case_pnl == pytest.approx(0.0)
    assert metrics.residual_up_qty == pytest.approx(50.0)
    assert metrics.residual_ev == pytest.approx(10.0)


def test_fill_that_improves_pair_cost_but_destroys_settlement_ev_is_rejected() -> None:
    balancer = PositionBalancer(VolatilityHedgeConfig(max_settlement_ev_worsening=1.0))
    pos = _position(100, 100, up_avg=0.80, down_avg=0.50)
    proposal = HedgeProposal(side="DOWN", price=0.10, qty=100, reason="cheap", confidence=0.8, features=_features(up_bid=0.94, up_ask=0.96, down_bid=0.04, down_ask=0.06))

    decision = balancer.evaluate(pos, proposal)

    assert decision.projected_paired_cost < decision.current_paired_cost
    assert decision.allowed is False
    assert decision.reason == "projected_settlement_ev_worse"


def test_repair_that_exceeds_worst_case_loss_is_rejected() -> None:
    balancer = PositionBalancer(VolatilityHedgeConfig(max_worst_case_loss_per_market=10.0, max_notional_per_market=2_000.0, max_notional_per_side=2_000.0))
    pos = _position(900, 300, up_avg=0.90, down_avg=0.10)

    decision = balancer.evaluate(pos, _proposal("DOWN", price=0.80, qty=100))

    assert decision.mode == PositionMode.REPAIR
    assert decision.allowed is False
    assert decision.reason == "worst_case_pnl_limit"


def test_seed_pair_cost_too_high_is_rejected_by_manager() -> None:
    from kalshibtc.execution.volatility_hedge import VolatilityHedgeManager

    manager = VolatilityHedgeManager(VolatilityHedgeConfig(slippage=0, require_seed_pair_cost_below=1.0, max_initial_ask_sum=1.02))
    decision = manager.on_book(
        event_key="event",
        market_ticker="ticker",
        ts=_ts(),
        market_close_time="close",
        strike=100_000,
        seconds_to_close=800,
        distance_from_strike=20,
        btc_price=100_020,
        slope=1,
        up_bid=0.70,
        up_ask=0.72,
        down_bid=0.31,
        down_ask=0.32,
        atr=10,
    )

    assert decision.fills == 0
    assert decision.reason == "early_seed_pair_cost_too_high"
    assert manager.positions == {}


def test_observe_phase_rejects_normal_seed_unless_ask_sum_at_or_below_one() -> None:
    manager, decision = _manager_decision(seconds_to_close=890, up_ask=0.57, down_ask=0.44)

    assert decision.fills == 0
    assert decision.reason == "observe_phase_seed_too_expensive"
    assert manager.events[-1]["lifecycle_phase"] == LifecyclePhase.OBSERVE
    assert manager.events[-1]["elapsed_seconds"] == pytest.approx(10.0)
    assert manager.events[-1]["simple_ask_sum"] == pytest.approx(1.01)


def test_early_seed_allows_tiny_starter_when_pair_cost_is_reasonable() -> None:
    manager, decision = _manager_decision(seconds_to_close=850, up_ask=0.53, down_ask=0.48)

    assert decision.fills == 2
    assert decision.reason == "initial_3_to_2_hedge"
    assert decision.projected_paired_cost == pytest.approx(1.01)
    position = manager.positions["ticker|close|100000.0"]
    assert position.total_cost <= VolatilityHedgeConfig().base_add_notional + 1e-9
    assert position.up_qty + position.down_qty <= VolatilityHedgeConfig().max_contracts_per_add
    assert {event["lifecycle_phase"] for event in manager.events} == {LifecyclePhase.EARLY_SEED}


def test_main_harvest_allows_tiny_seed_with_slight_loss_basis() -> None:
    manager, decision = _manager_decision(seconds_to_close=660, up_ask=0.85, down_ask=0.16)

    assert decision.fills == 2
    assert decision.projected_paired_cost == pytest.approx(1.01)
    assert manager.events[-1]["phase_seed_pair_threshold"] == pytest.approx(1.04)
    assert manager.events[-1]["phase_max_initial_ask_sum"] == pytest.approx(1.03)


def test_main_seed_rejects_when_projected_pair_cost_exceeds_phase_threshold() -> None:
    from kalshibtc.execution.volatility_hedge import VolatilityHedgeManager

    manager = VolatilityHedgeManager(
        VolatilityHedgeConfig(slippage=0, cooldown_seconds=0, max_initial_ask_sum=1.06, require_seed_pair_cost_below=1.04)
    )
    _, decision = _manager_decision(manager=manager, seconds_to_close=660, up_ask=0.88, down_ask=0.17)

    assert decision.fills == 0
    assert decision.reason == "main_seed_pair_cost_too_high"
    assert manager.positions == {}
    assert manager.events[-1]["seed_gate_failed_reason"] == "projected_seed_pair_cost_above_phase_threshold"


def test_seed_rejected_in_late_settlement_phase() -> None:
    manager, decision = _manager_decision(seconds_to_close=50, up_ask=0.49, down_ask=0.50)

    assert decision.fills == 0
    assert decision.reason == "late_settlement_no_new_seed"
    assert manager.events[-1]["lifecycle_phase"] == LifecyclePhase.LATE_SETTLEMENT
    assert manager.events[-1]["seed_window_open"] is False


def test_normal_add_rejected_after_normal_add_window_closes() -> None:
    from kalshibtc.execution.volatility_hedge import VolatilityHedgeManager

    manager = VolatilityHedgeManager(VolatilityHedgeConfig(slippage=0, cooldown_seconds=0))
    manager.positions["ticker|close|100000.0"] = _position(100, 100, up_avg=0.45, down_avg=0.50)

    _, decision = _manager_decision(manager=manager, seconds_to_close=80, up_ask=0.46, down_ask=0.45, offset=10)

    assert decision.fills == 0
    assert decision.reason == "normal_add_window_closed"
    assert manager.events[-1]["normal_add_window_open"] is False
    assert manager.events[-1]["repair_only"] is True


def test_late_repair_add_allowed_when_it_reduces_worst_case_pnl() -> None:
    from kalshibtc.execution.volatility_hedge import VolatilityHedgeManager

    manager = VolatilityHedgeManager(
        VolatilityHedgeConfig(
            slippage=0,
            cooldown_seconds=0,
            repair_add_notional=10.0,
            max_notional_per_market=250.0,
            max_notional_per_side=150.0,
        )
    )
    manager.positions["ticker|close|100000.0"] = _position(100, 10, up_avg=0.90, down_avg=0.10)

    _, decision = _manager_decision(manager=manager, seconds_to_close=50, up_ask=0.92, down_ask=0.02, offset=20)

    assert decision.fills == 1
    assert decision.fill_events[0].side == "DOWN"
    event = manager.events[-1]
    assert event["lifecycle_phase"] == LifecyclePhase.LATE_SETTLEMENT
    assert event["repair_only"] is True
    assert event["projected_worst_case_pnl"] > event["current_worst_case_pnl"]


def test_seed_rejected_early_can_still_seed_later_in_same_contract() -> None:
    from kalshibtc.execution.volatility_hedge import VolatilityHedgeManager

    manager = VolatilityHedgeManager(VolatilityHedgeConfig(slippage=0, cooldown_seconds=0))

    _, early = _manager_decision(manager=manager, seconds_to_close=850, up_ask=0.57, down_ask=0.46, offset=40)
    _, later = _manager_decision(manager=manager, seconds_to_close=660, up_ask=0.85, down_ask=0.16, offset=240)

    assert early.fills == 0
    assert early.reason == "early_seed_pair_cost_too_high"
    assert later.fills == 2
    assert later.reason == "initial_3_to_2_hedge"
    assert len({id(position) for position in manager.positions.values()}) == 1


def test_manager_rejects_invalid_event_key_without_opening_seed() -> None:
    manager, decision = _manager_decision(seconds_to_close=660, up_ask=0.49, down_ask=0.50, strike=15)

    assert decision.fills == 0
    assert decision.reason == "invalid_event_key"
    assert manager.positions == {}
    assert manager.events[-1]["reason"] == "invalid_event_key"


def test_canonical_cooldown_applies_when_caller_uses_noncanonical_event_key() -> None:
    from kalshibtc.execution.volatility_hedge import VolatilityHedgeManager

    manager = VolatilityHedgeManager(VolatilityHedgeConfig(slippage=0, cooldown_seconds=30))
    _, seed = _manager_decision(manager=manager, seconds_to_close=660, up_ask=0.85, down_ask=0.16, offset=100, event_key="legacy-event")
    _, add = _manager_decision(manager=manager, seconds_to_close=650, up_ask=0.80, down_ask=0.15, offset=105, event_key="legacy-event")

    assert seed.fills == 2
    assert add.fills == 0
    assert add.reason == "cooldown"


def test_expired_contract_blocks_late_repair_fill() -> None:
    from kalshibtc.execution.volatility_hedge import VolatilityHedgeManager

    manager = VolatilityHedgeManager(VolatilityHedgeConfig(slippage=0, cooldown_seconds=0))
    manager.positions["ticker|close|100000.0"] = _position(100, 10, up_avg=0.90, down_avg=0.10)

    _, decision = _manager_decision(manager=manager, seconds_to_close=0, up_ask=0.92, down_ask=0.02, offset=30)

    assert decision.fills == 0
    assert decision.reason == "expired_no_add"
    assert manager.events[-1]["time_to_expiry"] == pytest.approx(0.0)


def test_position_summary_uses_active_config_for_risk_metrics() -> None:
    position = _position(100, 50, up_avg=0.40, down_avg=0.20)
    row = position.summary_dict(VolatilityHedgeConfig(target_lean_ratio=1.25, max_notional_per_market=42.0))

    assert row["target_ratio"] == pytest.approx(1.25)
    assert row["max_market_notional"] == pytest.approx(42.0)
    assert row["notional_remaining"] == pytest.approx(42.0 - position.total_cost)


def test_strike_metadata_guard_rejects_ticker_suffix_as_btc_strike() -> None:
    with pytest.raises(ValueError, match="implausible BTC strike"):
        event_key_for_volatility_hedge("KXBTC15M-26MAY161130-30", "2026-05-16T15:30:00+00:00", 15.0)

    key = event_key_for_volatility_hedge("KXBTC15M-26MAY161130-30", "2026-05-16T15:30:00+00:00", 78095.76)
    assert key.endswith("|78095.76")
