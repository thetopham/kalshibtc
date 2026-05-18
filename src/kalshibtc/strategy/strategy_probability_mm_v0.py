from __future__ import annotations

from dataclasses import dataclass, field

from ..execution.paper import PaperFill
from ..market.state import MarketState
from ..probability.features import extract_probability_features
from ..probability.models import BrownianProbabilityModel
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
        self._model = BrownianProbabilityModel(min_volatility=self.config.min_volatility)
        self._balancer = InventoryBalancer(
            max_net_ratio=self.config.max_net_ratio,
            force_flatten_seconds=self.config.force_flatten_seconds,
        )

    @property
    def name(self) -> str:
        return self.config.name

    def on_tick(self, state: MarketState) -> Signal:
        if self._active_market_ticker != state.contract.ticker:
            self._active_market_ticker = state.contract.ticker
            self._yes_contracts = 0.0
            self._no_contracts = 0.0
        features = extract_probability_features(state)
        if state.seconds_to_close < self.config.min_seconds_to_close:
            return self._none("too close to expiry", features=features)
        regime_reason = self._regime_block(features)
        if regime_reason:
            return self._none(f"regime_filter: {regime_reason}", features=features)

        model_probability = self._model.predict_proba(
            distance_to_strike=features.distance_to_strike,
            volatility=max(features.atr, self.config.min_volatility),
            seconds_to_close=features.seconds_to_close,
        )
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
            return self._none(";".join(trade.blocked_by) or "no probability edge", features=features, model_probability=model_probability, trade=trade)

        if state.orderbook.yes_ask is None or state.orderbook.no_ask is None:
            return self._none("missing asks", features=features, model_probability=model_probability, trade=trade)
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
            return self._none(";".join(sizing.blocked_by) or sizing.reason, features=features, model_probability=model_probability, trade=trade)
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
            features=self._feature_payload(features, model_probability=model_probability, trade=trade),
            allow_price_strike_mismatch=True,
        )

    def on_fill(self, state: MarketState, fill: PaperFill) -> None:
        if fill.side == "long_above":
            self._yes_contracts += fill.contracts
        elif fill.side == "long_below":
            self._no_contracts += fill.contracts

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

    def _none(self, reason: str, *, features: object | None = None, model_probability: float | None = None, trade: object | None = None) -> Signal:
        payload = self._feature_payload(features, model_probability=model_probability, trade=trade) if features is not None else None
        return Signal("none", reason, 0.0, strategy=self.name, features=payload, allow_price_strike_mismatch=True)

    @staticmethod
    def _feature_payload(features: object | None, *, model_probability: float | None, trade: object | None) -> dict[str, float | bool | str | None]:
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
        if trade is not None:
            payload["market_probability"] = float(getattr(trade, "market_probability", 0.0))
            payload["edge"] = float(getattr(trade, "edge", 0.0))
            payload["ev_per_contract"] = float(getattr(trade, "ev_per_contract", 0.0))
            payload["probability_side"] = str(getattr(trade, "side", "none"))
        return payload
