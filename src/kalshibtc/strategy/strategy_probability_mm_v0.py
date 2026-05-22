from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..execution.paper import PaperFill
from ..market.state import MarketState
from ..probability.features import extract_probability_features
from ..probability.models import (
    BayesianMarkovTrendModel,
    BrownianProbabilityModel,
    MarkovTrendPrediction,
)
from ..probability.trading import (
    InventoryBalancer,
    ProbabilityTradeConfig,
    evaluate_probability_trade,
)
from .signals import Signal


@dataclass(frozen=True)
class StrategyProbabilityMMV0Config:
    name: str = "strategy_probability_mm_v0"
    edge_threshold: float = 0.03
    fee_rate: float = 0.0
    slippage: float = 0.0
    base_notional: float = 10.0
    max_net_ratio: float = 0.25
    force_flatten_seconds: float = 60.0
    min_seconds_to_close: float = 30.0
    max_wickiness: float = 0.75
    max_atr_slope: float = 1.5
    max_abs_ema_slope_flip: float = 5.0
    min_volatility: float = 1.0
    probability_model: str = "brownian"
    venue: str = "kalshi"
    markov_monte_carlo_paths: int = 500
    min_probability_confidence: float = 0.0
    min_abs_edge: float = 0.0
    max_probability_mid_band: float = 0.0


@dataclass
class StrategyProbabilityMMV0Strategy:
    """Probability-aware BTC above/below market maker prototype.

    v0 deliberately replaces static cheap-side accumulation with:
    - Brownian fair probability baseline from distance / volatility / sqrt(time)
    - EV gate after fee/slippage
    - inventory delta cap and forced flatten near expiry
    - coarse regime filter for wick/chop/vol expansion
    """

    config: StrategyProbabilityMMV0Config = field(default_factory=StrategyProbabilityMMV0Config)

    def __post_init__(self) -> None:
        self._active_market_ticker: str | None = None
        self._yes_contracts = 0.0
        self._no_contracts = 0.0
        self._kalshi_position_side: str | None = None
        self._kalshi_position_contracts = 0.0
        self._model = BrownianProbabilityModel(min_volatility=self.config.min_volatility)
        self._markov_model = BayesianMarkovTrendModel(
            monte_carlo_paths=self.config.markov_monte_carlo_paths,
            min_volatility=self.config.min_volatility,
        )
        self._balancer = InventoryBalancer(
            max_net_ratio=self.config.max_net_ratio,
            force_flatten_seconds=self.config.force_flatten_seconds,
            venue=self.config.venue,
        )

    @property
    def name(self) -> str:
        return self.config.name

    def on_tick(self, state: MarketState) -> Signal:
        if self._active_market_ticker != state.contract.ticker:
            self._active_market_ticker = state.contract.ticker
            self._yes_contracts = 0.0
            self._no_contracts = 0.0
            self._kalshi_position_side = None
            self._kalshi_position_contracts = 0.0
        features = extract_probability_features(state)
        if state.seconds_to_close < self.config.min_seconds_to_close:
            return self._none("too close to expiry", features=features)
        regime_reason = self._regime_block(features)
        if regime_reason:
            return self._none(f"regime_filter: {regime_reason}", features=features)

        model_probability, markov_prediction = self._predict_probability(state, features)
        trade = evaluate_probability_trade(
            model_probability=model_probability,
            yes_ask=state.orderbook.yes_ask,
            no_ask=state.orderbook.no_ask,
            config=ProbabilityTradeConfig(
                edge_threshold=self.config.edge_threshold,
                fee_rate=self.config.fee_rate,
                slippage=self.config.slippage,
                base_notional=self.config.base_notional,
            ),
        )
        if trade.side == "none":
            return self._none(";".join(trade.blocked_by) or "no probability edge", features=features, model_probability=model_probability, trade=trade, markov_prediction=markov_prediction)
        bucket_reason = self._bucket_block(model_probability=model_probability, trade=trade, markov_prediction=markov_prediction)
        if bucket_reason:
            return self._none(bucket_reason, features=features, model_probability=model_probability, trade=trade, markov_prediction=markov_prediction)

        if state.orderbook.yes_ask is None or state.orderbook.no_ask is None:
            return self._none("missing asks", features=features, model_probability=model_probability, trade=trade, markov_prediction=markov_prediction)
        if self.config.venue.lower() == "kalshi" and self._kalshi_position_side == trade.side:
            return self._none(
                "kalshi_single_position_already_open",
                features=features,
                model_probability=model_probability,
                trade=trade,
                markov_prediction=markov_prediction,
            )
        sizing = self._balancer.size_order(
            desired_side=trade.side,
            desired_notional=self.config.base_notional,
            yes_contracts=self._yes_contracts,
            no_contracts=self._no_contracts,
            yes_price=state.orderbook.yes_ask,
            no_price=state.orderbook.no_ask,
            seconds_to_close=state.seconds_to_close,
        )
        if not sizing.allowed:
            return self._none(";".join(sizing.blocked_by) or sizing.reason, features=features, model_probability=model_probability, trade=trade, markov_prediction=markov_prediction)
        side = "long_above" if sizing.side == "yes" else "long_below"
        price = state.orderbook.yes_ask if sizing.side == "yes" else state.orderbook.no_ask
        shares = sizing.notional / price if price and price > 0 else None
        return Signal(
            side=side,
            reason=sizing.reason,
            confidence=max(0.0, min(1.0, abs(trade.edge) * 10.0)),
            strategy=self.name,
            target_notional=round(sizing.notional, 6),
            estimated_shares=shares,
            features=self._feature_payload(features, model_probability=model_probability, trade=trade, markov_prediction=markov_prediction),
            allow_price_strike_mismatch=True,
        )

    def on_fill(self, state: MarketState, fill: PaperFill) -> None:
        if self.config.venue.lower() == "kalshi":
            side = "yes" if fill.side == "long_above" else "no" if fill.side == "long_below" else None
            if side is not None:
                self._kalshi_position_side = side
                self._kalshi_position_contracts = fill.contracts
                self._yes_contracts = fill.contracts if side == "yes" else 0.0
                self._no_contracts = fill.contracts if side == "no" else 0.0
            return
        if fill.side == "long_above":
            self._yes_contracts += fill.contracts
        elif fill.side == "long_below":
            self._no_contracts += fill.contracts

    def _predict_probability(self, state: MarketState, features: object) -> tuple[float, MarkovTrendPrediction | None]:
        if self.config.probability_model != "bayesian_markov":
            return (
                self._model.predict_proba(
                    distance_to_strike=features.distance_to_strike,
                    volatility=max(features.atr, self.config.min_volatility),
                    seconds_to_close=features.seconds_to_close,
                ),
                None,
            )
        raw = dict(state.tick.raw or {})
        raw.update(dict(state.orderbook.raw or {}))
        recent_returns = _recent_returns(raw)
        market_prior = _market_prior_yes(state.orderbook.yes_bid, state.orderbook.yes_ask)
        prediction = self._markov_model.predict_from_returns(
            recent_returns=recent_returns,
            current_price=state.price,
            strike=state.strike,
            seconds_to_close=state.seconds_to_close,
            market_prior_yes=market_prior,
        )
        return prediction.probability_yes, prediction

    def _bucket_block(self, *, model_probability: float, trade: object, markov_prediction: MarkovTrendPrediction | None) -> str | None:
        edge = abs(float(getattr(trade, "edge", 0.0)))
        if edge < self.config.min_abs_edge:
            return "edge_bucket_below_min_abs_edge"
        if markov_prediction is not None and markov_prediction.confidence < self.config.min_probability_confidence:
            return "probability_confidence_below_threshold"
        mid_band = abs(float(model_probability) - 0.5)
        if mid_band < self.config.max_probability_mid_band:
            return "probability_mid_band_blocked"
        return None

    def _regime_block(self, features: object) -> str | None:
        wickiness = float(getattr(features, "wickiness", 0.0))
        atr_slope = float(getattr(features, "atr_slope", 0.0))
        ema_slope = float(getattr(features, "ema_slope", 0.0))
        if wickiness > self.config.max_wickiness:
            return "high_wickiness"
        if atr_slope > self.config.max_atr_slope:
            return "volatility_expansion"
        if abs(ema_slope) > self.config.max_abs_ema_slope_flip and wickiness > self.config.max_wickiness * 0.75:
            return "rapid_slope_flip_chop"
        return None

    def _none(self, reason: str, *, features: object | None = None, model_probability: float | None = None, trade: object | None = None, markov_prediction: MarkovTrendPrediction | None = None) -> Signal:
        payload = self._feature_payload(features, model_probability=model_probability, trade=trade, markov_prediction=markov_prediction) if features is not None else None
        return Signal("none", reason, 0.0, strategy=self.name, features=payload, allow_price_strike_mismatch=True)

    @staticmethod
    def _feature_payload(features: object | None, *, model_probability: float | None, trade: object | None, markov_prediction: MarkovTrendPrediction | None = None) -> dict[str, float | bool | str | None]:
        payload: dict[str, float | bool | str | None] = {}
        if features is not None:
            for key in (
                "distance_to_strike",
                "seconds_to_close",
                "atr",
                "atr_slope",
                "realized_volatility",
                "ema_slope",
                "vwap_slope",
                "recent_momentum",
                "distance_from_vwap",
                "wickiness",
                "range_expansion",
                "orderbook_imbalance",
                "z_score",
            ):
                payload[key] = float(getattr(features, key))
        payload["model_probability"] = model_probability
        payload["probability_model"] = "bayesian_markov" if markov_prediction is not None else "brownian"
        if markov_prediction is not None:
            payload["bayes_model_probability"] = float(markov_prediction.model_probability_yes)
            payload["bayes_market_prior"] = markov_prediction.market_prior_yes
            payload["bayes_confidence"] = float(markov_prediction.confidence)
            for regime, probability in markov_prediction.regime_probabilities.items():
                payload[f"bayes_regime_{regime}"] = float(probability)
        if trade is not None:
            payload["market_probability"] = float(getattr(trade, "market_probability", 0.0))
            payload["edge"] = float(getattr(trade, "edge", 0.0))
            payload["ev_per_contract"] = float(getattr(trade, "ev_per_contract", 0.0))
            payload["probability_side"] = str(getattr(trade, "side", "none"))
        return payload


def _recent_returns(raw: dict[str, object]) -> list[float]:
    value = raw.get("recent_returns")
    if isinstance(value, str):
        parts: Sequence[object] = value.split(",")
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        parts = value
    else:
        parts = (
            raw.get("return_1s"),
            raw.get("return_2s"),
            raw.get("return_3s"),
            raw.get("recent_momentum"),
        )
    returns: list[float] = []
    for part in parts:
        if part is None or part == "":
            continue
        try:
            returns.append(float(part))
        except (TypeError, ValueError):
            continue
    return returns


def _market_prior_yes(yes_bid: float | None, yes_ask: float | None) -> float | None:
    if yes_bid is None or yes_ask is None or yes_bid <= 0 or yes_ask <= 0:
        return None
    return max(0.0, min(1.0, (float(yes_bid) + float(yes_ask)) / 2.0))
