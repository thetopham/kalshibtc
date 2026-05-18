from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kalshibtc.datafeed.models import OrderBookSnapshot, Tick
from kalshibtc.market.contract import ContractWindow
from kalshibtc.market.state import MarketState
from kalshibtc.probability.features import build_probability_example, extract_probability_features
from kalshibtc.probability.models import (
    BrownianProbabilityModel,
    LogisticProbabilityModel,
    fit_isotonic_calibrator,
)
from kalshibtc.probability.trading import (
    InventoryBalancer,
    ProbabilityTradeConfig,
    evaluate_probability_trade,
)
from kalshibtc.strategy.registry import create_strategy, strategy_names
from kalshibtc.strategy.strategy_probability_mm_v0 import (
    StrategyProbabilityMMV0Config,
    StrategyProbabilityMMV0Strategy,
)


def _state(
    *,
    price: float = 100_020.0,
    strike: float = 100_000.0,
    seconds_to_close: float = 300.0,
    yes_bid: float = 0.52,
    yes_ask: float = 0.54,
    no_bid: float = 0.46,
    no_ask: float = 0.48,
    raw: dict | None = None,
) -> MarketState:
    ts = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    contract = ContractWindow(
        ticker="btc-updown-15m-test",
        strike=strike,
        open_time=ts - timedelta(minutes=10),
        close_time=ts + timedelta(seconds=seconds_to_close),
    )
    return MarketState(
        tick=Tick(ts=ts, price=price, source="test", symbol="BTC-USD", raw=raw or {}),
        orderbook=OrderBookSnapshot(
            ts=ts,
            market_ticker=contract.ticker,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            raw=raw or {},
        ),
        contract=contract,
        slope_30s=(raw or {}).get("ema_slope", 0.0),
    )


def test_probability_feature_extraction_computes_z_score_and_microstructure_inputs() -> None:
    state = _state(
        price=100_050.0,
        strike=100_000.0,
        seconds_to_close=225.0,
        yes_bid=0.54,
        yes_ask=0.56,
        no_bid=0.43,
        no_ask=0.45,
        raw={
            "atr_60s": 25.0,
            "atr_slope": 0.12,
            "realized_volatility": 0.0015,
            "ema_slope": 2.5,
            "vwap": 100_020.0,
            "vwap_slope": 1.25,
            "recent_momentum": 18.0,
            "wickiness": 0.35,
            "range_expansion": 1.4,
            "yes_bid_size": 120.0,
            "yes_ask_size": 80.0,
            "no_bid_size": 60.0,
            "no_ask_size": 100.0,
        },
    )

    features = extract_probability_features(state)

    assert features.distance_to_strike == 50.0
    assert features.seconds_to_close == 225.0
    assert features.atr == 25.0
    assert features.distance_from_vwap == 30.0
    assert features.orderbook_imbalance == pytest.approx(40.0 / 360.0)
    assert features.z_score == pytest.approx(50.0 / (25.0 * 15.0))


def test_probability_labeling_uses_final_outcome_and_realized_move() -> None:
    state = _state(price=100_010.0, strike=100_000.0, seconds_to_close=120.0)

    example = build_probability_example(state, final_price=99_975.0)

    assert example.label_finish_above is False
    assert example.realized_move_to_settlement == -35.0
    assert example.features.distance_to_strike == 10.0


def test_brownian_probability_baseline_is_monotonic_around_strike() -> None:
    model = BrownianProbabilityModel(min_volatility=1.0)
    below = model.predict_proba(distance_to_strike=-20.0, volatility=10.0, seconds_to_close=300.0)
    at = model.predict_proba(distance_to_strike=0.0, volatility=10.0, seconds_to_close=300.0)
    above = model.predict_proba(distance_to_strike=20.0, volatility=10.0, seconds_to_close=300.0)

    assert below < at < above
    assert at == pytest.approx(0.5)


def test_logistic_and_isotonic_calibration_fit_small_labeled_dataset() -> None:
    rows = [
        ([-2.0, 0.1], False),
        ([-1.0, 0.1], False),
        ([0.2, 0.0], True),
        ([1.0, -0.1], True),
        ([2.0, 0.2], True),
    ]
    model = LogisticProbabilityModel(learning_rate=0.2, iterations=500).fit(rows)
    raw_probs = [model.predict_vector(x) for x, _ in rows]
    calibrator = fit_isotonic_calibrator(raw_probs, [y for _, y in rows])

    assert model.predict_vector([1.5, 0.0]) > model.predict_vector([-1.5, 0.0])
    assert calibrator.predict(0.1) <= calibrator.predict(0.9)


def test_ev_gate_requires_edge_after_fee_and_slippage() -> None:
    decision = evaluate_probability_trade(
        model_probability=0.62,
        yes_ask=0.55,
        no_ask=0.47,
        config=ProbabilityTradeConfig(edge_threshold=0.03, fee_rate=0.01, slippage=0.01),
    )

    assert decision.side == "yes"
    assert decision.market_probability == 0.55
    assert decision.edge == pytest.approx(0.07)
    assert decision.ev_per_contract > 0

    weak = evaluate_probability_trade(
        model_probability=0.57,
        yes_ask=0.55,
        no_ask=0.47,
        config=ProbabilityTradeConfig(edge_threshold=0.03, fee_rate=0.01, slippage=0.01),
    )
    assert weak.side == "none"
    assert "edge_below_threshold" in weak.blocked_by


def test_inventory_balancer_caps_delta_and_forces_flatten_near_expiry() -> None:
    balancer = InventoryBalancer(max_net_ratio=0.2, force_flatten_seconds=45.0)

    sizing = balancer.size_order(
        desired_side="yes",
        desired_notional=50.0,
        yes_contracts=100.0,
        no_contracts=80.0,
        yes_price=0.55,
        no_price=0.47,
        seconds_to_close=180.0,
    )
    assert sizing.allowed is False
    assert "max_net_ratio" in sizing.blocked_by

    flatten = balancer.size_order(
        desired_side="yes",
        desired_notional=50.0,
        yes_contracts=100.0,
        no_contracts=40.0,
        yes_price=0.55,
        no_price=0.47,
        seconds_to_close=30.0,
    )
    assert flatten.allowed is True
    assert flatten.side == "no"
    assert flatten.reason == "forced flatten near expiry"


def test_strategy_probability_mm_v0_registers_and_emits_ev_positive_signal() -> None:
    assert "strategy_probability_mm_v0" in strategy_names()
    strategy = create_strategy(
        "strategy_probability_mm_v0",
        {"edge_threshold": 0.02, "min_seconds_to_close": 60.0, "max_net_ratio": 0.35},
    )
    assert isinstance(strategy, StrategyProbabilityMMV0Strategy)

    signal = strategy.on_tick(
        _state(
            price=100_080.0,
            strike=100_000.0,
            seconds_to_close=300.0,
            yes_bid=0.50,
            yes_ask=0.52,
            no_bid=0.47,
            no_ask=0.49,
            raw={"atr_60s": 10.0, "realized_volatility": 0.001, "ema_slope": 1.0},
        )
    )

    assert signal.side == "long_above"
    assert signal.strategy == "strategy_probability_mm_v0"
    assert signal.features is not None
    assert signal.features["model_probability"] > signal.features["market_probability"]
    assert signal.features["ev_per_contract"] > 0


def test_strategy_probability_mm_v0_blocks_chop_and_high_wickiness() -> None:
    strategy = StrategyProbabilityMMV0Strategy(StrategyProbabilityMMV0Config(max_wickiness=0.6))

    signal = strategy.on_tick(
        _state(
            price=100_080.0,
            strike=100_000.0,
            seconds_to_close=300.0,
            yes_bid=0.50,
            yes_ask=0.52,
            raw={"atr_60s": 10.0, "wickiness": 0.9, "ema_slope": -3.0, "atr_slope": 0.5},
        )
    )

    assert signal.side == "none"
    assert "regime_filter" in signal.reason
