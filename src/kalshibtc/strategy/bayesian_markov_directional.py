from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ..execution.paper import PaperFill
from ..market.state import MarketState
from ..probability.features import extract_probability_features
from ..probability.models import BayesianMarkovTrendModel, MarkovTrendPrediction
from ..probability.trading import (
    ProbabilityTradeConfig,
    ProbabilityTradeDecision,
    evaluate_probability_trade,
)
from .signals import Signal


@dataclass(frozen=True)
class BayesianMarkovDirectionalConfig:
    """Kalshi-only single-position Bayesian/Markov directional strategy.

    This is intentionally not a market-making, hedge, pair, repair, or inventory
    strategy. Kalshi mode holds at most one logical side per market. A same-side
    repeat is blocked; an opposite-side signal means exit/flip the current
    position, not simultaneous YES+NO inventory.
    """

    name: str = "bayesian_markov_directional"
    edge_threshold: float = 0.05
    fee_rate: float = 0.0
    slippage: float = 0.0
    base_notional: float = 10.0
    min_seconds_to_close: float = 60.0
    min_volatility: float = 1.0
    min_confidence: float = 0.0
    min_abs_edge: float = 0.0
    max_probability_mid_band: float = 0.0
    max_wickiness: float = 0.75
    max_atr_slope: float = 1.5
    max_abs_ema_slope_flip: float = 5.0
    markov_monte_carlo_paths: int = 500


@dataclass
class BayesianMarkovDirectionalStrategy:
    config: BayesianMarkovDirectionalConfig = field(default_factory=BayesianMarkovDirectionalConfig)

    def __post_init__(self) -> None:
        self._active_market_ticker: str | None = None
        self._position_side: str | None = None
        self._position_contracts = 0.0
        self._model = BayesianMarkovTrendModel(
            monte_carlo_paths=self.config.markov_monte_carlo_paths,
            min_volatility=self.config.min_volatility,
        )

    @property
    def name(self) -> str:
        return self.config.name

    def on_tick(self, state: MarketState) -> Signal:
        if self._active_market_ticker != state.contract.ticker:
            self._active_market_ticker = state.contract.ticker
            self._position_side = None
            self._position_contracts = 0.0

        features = extract_probability_features(state)
        if state.seconds_to_close < self.config.min_seconds_to_close:
            return self._none("too close to expiry", features=features)
        regime_reason = self._regime_block(features)
        if regime_reason:
            return self._none(f"regime_filter: {regime_reason}", features=features)
        if state.orderbook.yes_ask is None or state.orderbook.no_ask is None:
            return self._none("missing asks", features=features)

        prediction = self._predict(state)
        trade = evaluate_probability_trade(
            model_probability=prediction.probability_yes,
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
            return self._none(
                ";".join(trade.blocked_by) or "no bayesian markov edge",
                features=features,
                prediction=prediction,
                trade=trade,
            )

        bucket_reason = self._bucket_block(prediction=prediction, trade=trade)
        if bucket_reason:
            return self._none(bucket_reason, features=features, prediction=prediction, trade=trade)

        if self._position_side == trade.side:
            return self._none("kalshi_single_position_already_open", features=features, prediction=prediction, trade=trade)

        side = "long_above" if trade.side == "yes" else "long_below"
        price = state.orderbook.yes_ask if trade.side == "yes" else state.orderbook.no_ask
        shares = self.config.base_notional / price if price and price > 0 else None
        action = "open" if self._position_side is None else f"flip_from_{self._position_side}_to_{trade.side}"
        return Signal(
            side=side,
            reason=f"bayesian_markov_directional_{action}",
            confidence=max(0.0, min(1.0, prediction.confidence + abs(trade.edge))),
            strategy=self.name,
            target_notional=self.config.base_notional,
            estimated_shares=shares,
            features=self._feature_payload(features, prediction=prediction, trade=trade),
            allow_price_strike_mismatch=True,
        )

    def on_fill(self, state: MarketState, fill: PaperFill) -> None:
        side = "yes" if fill.side == "long_above" else "no" if fill.side == "long_below" else None
        if side is None:
            return
        if self._active_market_ticker != state.contract.ticker:
            self._active_market_ticker = state.contract.ticker
        self._position_side = side
        self._position_contracts = fill.contracts

    def _predict(self, state: MarketState) -> MarkovTrendPrediction:
        raw = dict(state.tick.raw or {})
        raw.update(dict(state.orderbook.raw or {}))
        return self._model.predict_from_returns(
            recent_returns=_recent_returns(raw),
            current_price=state.price,
            strike=state.strike,
            seconds_to_close=state.seconds_to_close,
            market_prior_yes=_market_prior_yes(state.orderbook.yes_bid, state.orderbook.yes_ask),
        )

    def _bucket_block(self, *, prediction: MarkovTrendPrediction, trade: ProbabilityTradeDecision) -> str | None:
        edge = abs(float(trade.edge))
        if edge < self.config.min_abs_edge:
            return "edge_below_min_abs_edge"
        if prediction.confidence < self.config.min_confidence:
            return "bayesian_confidence_below_threshold"
        mid_band = abs(float(prediction.probability_yes) - 0.5)
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

    def _none(
        self,
        reason: str,
        *,
        features: object | None = None,
        prediction: MarkovTrendPrediction | None = None,
        trade: ProbabilityTradeDecision | None = None,
    ) -> Signal:
        payload = self._feature_payload(features, prediction=prediction, trade=trade) if features is not None else None
        return Signal("none", reason, 0.0, strategy=self.name, features=payload, allow_price_strike_mismatch=True)

    @staticmethod
    def _feature_payload(
        features: object | None,
        *,
        prediction: MarkovTrendPrediction | None,
        trade: ProbabilityTradeDecision | None,
    ) -> dict[str, float | bool | str | None]:
        payload: dict[str, float | bool | str | None] = {"probability_model": "bayesian_markov"}
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
        if prediction is not None:
            payload["model_probability"] = float(prediction.probability_yes)
            payload["bayes_model_probability"] = float(prediction.model_probability_yes)
            payload["bayes_market_prior"] = prediction.market_prior_yes
            payload["bayes_confidence"] = float(prediction.confidence)
            for regime, probability in prediction.regime_probabilities.items():
                payload[f"bayes_regime_{regime}"] = float(probability)
        if trade is not None:
            payload["market_probability"] = float(trade.market_probability)
            payload["edge"] = float(trade.edge)
            payload["ev_per_contract"] = float(trade.ev_per_contract)
            payload["probability_side"] = trade.side
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
            returns.append(float(str(part)))
        except (TypeError, ValueError):
            continue
    return returns


def _market_prior_yes(yes_bid: float | None, yes_ask: float | None) -> float | None:
    if yes_bid is None or yes_ask is None or yes_bid <= 0 or yes_ask <= 0:
        return None
    return max(0.0, min(1.0, (float(yes_bid) + float(yes_ask)) / 2.0))
