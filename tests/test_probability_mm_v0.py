from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kalshibtc.datafeed.models import OrderBookSnapshot, Tick
from kalshibtc.execution.paper import PaperFill
from kalshibtc.market.contract import ContractWindow
from kalshibtc.market.state import MarketState
from kalshibtc.probability.features import build_probability_example, extract_probability_features
from kalshibtc.probability.models import (
    BayesianMarkovTrendModel,
    BrownianProbabilityModel,
    LogisticProbabilityModel,
    SyntheticMarkovConfig,
    SyntheticMarkovPathGenerator,
    distance_probability,
    fit_isotonic_calibrator,
    probability_yes_from_z,
)
from kalshibtc.probability.trading import (
    InventoryBalancer,
    ProbabilityTradeConfig,
    evaluate_probability_trade,
)
from kalshibtc.strategy.bayesian_markov_directional import (
    BayesianMarkovDirectionalConfig,
    BayesianMarkovDirectionalStrategy,
)
from kalshibtc.strategy.late_lotto_ticket import LateLottoTicketConfig, LateLottoTicketStrategy
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


def _paper_fill(*, side: str, contracts: float, notional: float) -> PaperFill:
    return PaperFill(
        strategy="strategy_probability_mm_v0",
        market_ticker="btc-updown-15m-test",
        side=side,
        entry_price=notional / contracts,
        notional=notional,
        contracts=contracts,
        ts=datetime(2026, 5, 18, 12, 0, tzinfo=UTC),
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


def test_z_probability_maps_to_yes_no_normal_cdf() -> None:
    assert probability_yes_from_z(0.0) == pytest.approx(0.5)
    assert probability_yes_from_z(1.0) == pytest.approx(0.841344746, rel=1e-6)
    assert probability_yes_from_z(-1.0) == pytest.approx(0.158655254, rel=1e-6)

    probability = distance_probability(
        distance_to_strike=50.0,
        sigma_per_sqrt_second=10.0,
        seconds_to_expiry=25.0,
    )

    assert probability.z_score == pytest.approx(1.0)
    assert probability.probability_yes == pytest.approx(0.841344746, rel=1e-6)
    assert probability.probability_no == pytest.approx(0.158655254, rel=1e-6)


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


def test_inventory_balancer_caps_delta_and_forces_flatten_near_expiry_for_polymarket() -> None:
    balancer = InventoryBalancer(max_net_ratio=0.2, force_flatten_seconds=45.0, venue="polymarket")

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


def test_inventory_balancer_is_directional_only_on_kalshi() -> None:
    balancer = InventoryBalancer(max_net_ratio=0.2, force_flatten_seconds=45.0, venue="kalshi")

    sizing = balancer.size_order(
        desired_side="yes",
        desired_notional=50.0,
        yes_contracts=100.0,
        no_contracts=80.0,
        yes_price=0.55,
        no_price=0.47,
        seconds_to_close=180.0,
    )
    assert sizing.allowed is True
    assert sizing.side == "yes"
    assert sizing.reason == "kalshi directional probability edge sized"

    near_expiry = balancer.size_order(
        desired_side="yes",
        desired_notional=50.0,
        yes_contracts=100.0,
        no_contracts=40.0,
        yes_price=0.55,
        no_price=0.47,
        seconds_to_close=30.0,
    )
    assert near_expiry.allowed is True
    assert near_expiry.side == "yes"
    assert near_expiry.reason == "kalshi directional probability edge sized"


def test_strategy_probability_mm_v0_registers_and_emits_ev_positive_signal() -> None:
    assert "strategy_probability_mm_v0" in strategy_names()
    strategy = create_strategy(
        "strategy_probability_mm_v0",
        {"edge_threshold": 0.02, "min_seconds_to_close": 60.0, "max_net_ratio": 0.35},
    )
    assert isinstance(strategy, StrategyProbabilityMMV0Strategy)
    assert strategy.config.venue == "kalshi"

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


def test_strategy_probability_mm_v0_blocks_same_side_reentry_on_kalshi() -> None:
    strategy = StrategyProbabilityMMV0Strategy(
        StrategyProbabilityMMV0Config(
            venue="kalshi",
            probability_model="bayesian_markov",
            edge_threshold=0.02,
            min_seconds_to_close=60.0,
            min_volatility=1.0,
        )
    )
    state = _state(
        price=100_020.0,
        strike=100_000.0,
        seconds_to_close=240.0,
        yes_bid=0.45,
        yes_ask=0.47,
        no_bid=0.52,
        no_ask=0.54,
        raw={"atr_60s": 2.0, "recent_returns": [2.1, 1.7, 2.2, 1.9, 2.4], "ema_slope": 2.0},
    )

    first = strategy.on_tick(state)
    assert first.side == "long_above"
    strategy.on_fill(
        state,
        _paper_fill(side="long_above", contracts=10.0, notional=4.7),
    )

    second = strategy.on_tick(state)
    assert second.side == "none"
    assert second.reason == "kalshi_single_position_already_open"


def test_bayesian_markov_directional_trades_and_blocks_same_side_reentry() -> None:
    strategy = BayesianMarkovDirectionalStrategy(
        BayesianMarkovDirectionalConfig(
            edge_threshold=0.02,
            min_seconds_to_close=60.0,
            min_volatility=1.0,
        )
    )
    state = _state(
        price=100_020.0,
        strike=100_000.0,
        seconds_to_close=240.0,
        yes_bid=0.45,
        yes_ask=0.47,
        no_bid=0.52,
        no_ask=0.54,
        raw={"atr_60s": 2.0, "recent_returns": [2.1, 1.7, 2.2, 1.9, 2.4], "ema_slope": 2.0},
    )

    first = strategy.on_tick(state)
    assert first.side == "long_above"
    assert first.reason == "bayesian_markov_directional_open"
    assert first.features is not None
    assert first.features["probability_model"] == "bayesian_markov"
    assert float(first.features["bayes_regime_up"] or 0.0) > float(first.features["bayes_regime_down"] or 0.0)

    strategy.on_fill(state, _paper_fill(side="long_above", contracts=10.0, notional=4.7))
    second = strategy.on_tick(state)
    assert second.side == "none"
    assert second.reason == "kalshi_single_position_already_open"


def test_bayesian_markov_directional_opposite_side_is_flip_not_hedge() -> None:
    strategy = BayesianMarkovDirectionalStrategy(BayesianMarkovDirectionalConfig(edge_threshold=0.02, min_seconds_to_close=60.0))
    up_state = _state(
        price=100_020.0,
        strike=100_000.0,
        seconds_to_close=240.0,
        yes_bid=0.45,
        yes_ask=0.47,
        no_bid=0.52,
        no_ask=0.54,
        raw={"atr_60s": 2.0, "recent_returns": [2.1, 1.7, 2.2, 1.9, 2.4], "ema_slope": 2.0},
    )
    strategy.on_fill(up_state, _paper_fill(side="long_above", contracts=10.0, notional=4.7))

    down_state = _state(
        price=99_980.0,
        strike=100_000.0,
        seconds_to_close=240.0,
        yes_bid=0.45,
        yes_ask=0.50,
        no_bid=0.42,
        no_ask=0.44,
        raw={"atr_60s": 2.0, "recent_returns": [-2.1, -1.7, -2.2, -1.9, -2.4], "ema_slope": -2.0},
    )
    flip = strategy.on_tick(down_state)
    assert flip.side == "long_below"
    assert flip.reason == "bayesian_markov_directional_flip_from_yes_to_no"

    strategy.on_fill(down_state, _paper_fill(side="long_below", contracts=10.0, notional=4.4))
    repeat = strategy.on_tick(down_state)
    assert repeat.side == "none"
    assert repeat.reason == "kalshi_single_position_already_open"


def test_late_lotto_ticket_buys_cheapest_side_one_to_two_minutes_from_expiry() -> None:
    strategy = LateLottoTicketStrategy(
        LateLottoTicketConfig(
            min_seconds_to_close=60.0,
            max_seconds_to_close=120.0,
            max_ticket_price=0.02,
            base_notional=10.0,
        )
    )
    state = _state(
        seconds_to_close=90.0,
        yes_bid=0.98,
        yes_ask=0.99,
        no_bid=0.01,
        no_ask=0.015,
    )

    signal = strategy.on_tick(state)

    assert signal.side == "long_below"
    assert signal.reason == "late_lotto_ticket_cheapest_no"
    assert signal.target_notional == 10.0
    assert signal.estimated_shares == pytest.approx(10.0 / 0.015)
    assert signal.features is not None
    assert signal.features["lotto_ticket_price"] == 0.015
    assert signal.allow_price_strike_mismatch is True


def test_late_lotto_ticket_blocks_outside_window_and_same_market_repeat() -> None:
    strategy = LateLottoTicketStrategy(
        LateLottoTicketConfig(min_seconds_to_close=60.0, max_seconds_to_close=120.0, max_ticket_price=0.02)
    )
    too_early = _state(seconds_to_close=180.0, yes_ask=0.01, no_ask=0.02)
    tradable = _state(seconds_to_close=90.0, yes_ask=0.01, no_ask=0.02)

    assert strategy.on_tick(too_early).reason == "outside_lotto_window"
    first = strategy.on_tick(tradable)
    assert first.side == "long_above"
    strategy.on_fill(tradable, _paper_fill(side="long_above", contracts=1000.0, notional=10.0))

    repeat = strategy.on_tick(tradable)
    assert repeat.side == "none"
    assert repeat.reason == "late_lotto_ticket_already_open"


def test_late_lotto_ticket_can_restrict_to_no_only() -> None:
    strategy = LateLottoTicketStrategy(
        LateLottoTicketConfig(
            min_seconds_to_close=60.0,
            max_seconds_to_close=120.0,
            max_ticket_price=0.02,
            side_mode="no_only",
        )
    )
    state = _state(seconds_to_close=90.0, yes_ask=0.001, no_ask=0.02)

    signal = strategy.on_tick(state)

    assert signal.side == "long_below"
    assert signal.reason == "late_lotto_ticket_cheapest_no"
    assert signal.features is not None
    assert signal.features["side_mode"] == "no_only"


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


def test_synthetic_markov_generator_is_reproducible_and_labels_hidden_states() -> None:
    config = SyntheticMarkovConfig(
        start_price=100_000.0,
        seconds=12,
        transition_matrix={
            "up": {"up": 0.92, "down": 0.04, "chop": 0.04},
            "down": {"up": 0.04, "down": 0.92, "chop": 0.04},
            "chop": {"up": 0.08, "down": 0.08, "chop": 0.84},
        },
        drift_by_state={"up": 4.0, "down": -4.0, "chop": 0.0},
        volatility_by_state={"up": 0.5, "down": 0.5, "chop": 2.0},
        initial_state="up",
        seed=7,
    )

    first = SyntheticMarkovPathGenerator(config).generate()
    second = SyntheticMarkovPathGenerator(config).generate()

    assert first.prices == second.prices
    assert first.states == second.states
    assert len(first.prices) == 13
    assert len(first.states) == 12
    assert set(first.states) <= {"up", "down", "chop"}
    assert first.prices[-1] > first.prices[0]


def test_bayesian_markov_trend_model_updates_regime_posterior_and_probability() -> None:
    model = BayesianMarkovTrendModel(
        transition_matrix={
            "up": {"up": 0.95, "down": 0.02, "chop": 0.03},
            "down": {"up": 0.02, "down": 0.95, "chop": 0.03},
            "chop": {"up": 0.05, "down": 0.05, "chop": 0.90},
        },
        drift_by_state={"up": 2.0, "down": -2.0, "chop": 0.0},
        volatility_by_state={"up": 1.0, "down": 1.0, "chop": 4.0},
        monte_carlo_paths=250,
        random_seed=11,
    )

    up_result = model.predict_from_returns(
        recent_returns=[1.8, 2.1, 1.6, 2.4, 1.9],
        current_price=100_010.0,
        strike=100_000.0,
        seconds_to_close=120.0,
        market_prior_yes=0.52,
    )
    down_result = model.predict_from_returns(
        recent_returns=[-1.8, -2.2, -1.5, -2.4, -1.9],
        current_price=99_990.0,
        strike=100_000.0,
        seconds_to_close=120.0,
        market_prior_yes=0.48,
    )

    assert up_result.regime_probabilities["up"] > up_result.regime_probabilities["down"]
    assert down_result.regime_probabilities["down"] > down_result.regime_probabilities["up"]
    assert up_result.probability_yes > 0.52
    assert down_result.probability_yes < 0.48
    assert 0.0 <= up_result.confidence <= 1.0
    assert up_result.model_probability_yes != up_result.market_prior_yes


def test_strategy_probability_mm_v0_can_use_bayesian_markov_model_with_regime_payload() -> None:
    strategy = StrategyProbabilityMMV0Strategy(
        StrategyProbabilityMMV0Config(
            probability_model="bayesian_markov",
            edge_threshold=0.02,
            min_seconds_to_close=60.0,
            min_volatility=1.0,
        )
    )

    signal = strategy.on_tick(
        _state(
            price=100_020.0,
            strike=100_000.0,
            seconds_to_close=240.0,
            yes_bid=0.45,
            yes_ask=0.47,
            no_bid=0.52,
            no_ask=0.54,
            raw={
                "atr_60s": 2.0,
                "recent_returns": [2.1, 1.7, 2.2, 1.9, 2.4],
                "ema_slope": 2.0,
            },
        )
    )

    assert signal.side == "long_above"
    assert signal.features is not None
    assert signal.features["probability_model"] == "bayesian_markov"
    assert signal.features["bayes_regime_up"] > signal.features["bayes_regime_down"]
    assert signal.features["bayes_confidence"] > 0.0


def test_strategy_probability_mm_v0_blocks_weak_probability_and_edge_buckets() -> None:
    weak_probability_strategy = StrategyProbabilityMMV0Strategy(
        StrategyProbabilityMMV0Config(
            probability_model="bayesian_markov",
            edge_threshold=0.02,
            min_probability_confidence=0.85,
            min_seconds_to_close=60.0,
        )
    )

    weak_probability = weak_probability_strategy.on_tick(
        _state(
            price=100_020.0,
            strike=100_000.0,
            seconds_to_close=240.0,
            yes_bid=0.45,
            yes_ask=0.47,
            raw={"atr_60s": 2.0, "recent_returns": [0.1, -0.1, 0.0, 0.1], "ema_slope": 0.0},
        )
    )

    assert weak_probability.side == "none"
    assert "probability_confidence_below_threshold" in weak_probability.reason

    weak_edge_strategy = StrategyProbabilityMMV0Strategy(
        StrategyProbabilityMMV0Config(
            probability_model="bayesian_markov",
            edge_threshold=0.02,
            min_abs_edge=0.50,
            min_seconds_to_close=60.0,
        )
    )

    weak_edge = weak_edge_strategy.on_tick(
        _state(
            price=100_020.0,
            strike=100_000.0,
            seconds_to_close=240.0,
            yes_bid=0.45,
            yes_ask=0.47,
            no_ask=0.50,
            raw={"atr_60s": 8.0, "recent_returns": [1.0, 0.8, 1.1, 0.9, 1.0], "ema_slope": 1.0},
        )
    )

    assert weak_edge.side == "none"
    assert "edge_bucket_below_min_abs_edge" in weak_edge.reason
