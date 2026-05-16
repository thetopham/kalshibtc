from __future__ import annotations

from dataclasses import dataclass, field

from ..market.pricing import entry_price_for_signal
from ..market.state import MarketState
from .signals import Signal
from .volatility_features import VolatilityFeatureBuilder, VolatilityFeatures


@dataclass(frozen=True)
class VolatilityInventoryConfig:
    name: str = "volatility_inventory"
    min_atr_1m: float = 5.0
    expansion_velocity_threshold: float = 1.0
    slowdown_threshold: float = 1.0
    compression_seconds_to_expiry: float = 300.0
    min_distance_from_strike: float = 25.0
    base_notional: float = 25.0
    max_notional_per_add: float = 250.0
    floor_price: float = 0.05
    min_confidence: float = 0.65
    low_atr_multiplier: float = 0.25
    normal_atr_multiplier: float = 1.0
    high_atr_multiplier: float = 2.0
    extreme_atr_multiplier: float = 4.0
    near_distance_multiplier: float = 0.5
    moderate_distance_multiplier: float = 1.0
    large_distance_multiplier: float = 2.0
    flat_velocity_multiplier: float = 0.0
    normal_velocity_multiplier: float = 1.0
    fast_velocity_multiplier: float = 2.0
    slowdown_velocity_multiplier: float = 1.0


@dataclass
class InventoryLot:
    side: str
    qty: float
    avg_price: float

    @property
    def notional(self) -> float:
        return self.qty * self.avg_price

    def add(self, *, qty: float, price: float) -> None:
        total_notional = self.notional + qty * price
        self.qty += qty
        self.avg_price = total_notional / self.qty if self.qty > 0 else 0.0


@dataclass
class InventorySnapshot:
    ts: str
    market_ticker: str
    above_qty: float
    above_avg_price: float
    below_qty: float
    below_avg_price: float
    blended_basis: float | None
    imbalance_ratio: float


@dataclass
class EquitySnapshot:
    ts: str
    market_ticker: str
    mtm_value: float
    cost_basis: float
    unrealized_pnl: float
    above_mark: float | None
    below_mark: float | None


@dataclass
class InventoryDecision:
    ts: str
    market_ticker: str
    side: str
    reason: str
    target_notional: float
    estimated_shares: float
    entry_price: float | None
    features: VolatilityFeatures


@dataclass
class VolatilityInventoryStrategy:
    """Paper-only volatility-regime inventory strategy.

    It never submits orders. It emits inventory add signals sized by notional and records
    feature/position/equity telemetry for replay research.
    """

    config: VolatilityInventoryConfig = field(default_factory=VolatilityInventoryConfig)

    def __post_init__(self) -> None:
        self._features = VolatilityFeatureBuilder(
            min_atr_1m=self.config.min_atr_1m,
            expansion_velocity_threshold=self.config.expansion_velocity_threshold,
            slowdown_threshold=self.config.slowdown_threshold,
            compression_seconds_to_expiry=self.config.compression_seconds_to_expiry,
        )
        self._positions: dict[str, InventoryLot] = {
            "long_above": InventoryLot("long_above", 0.0, 0.0),
            "long_below": InventoryLot("long_below", 0.0, 0.0),
        }
        self.feature_history: list[VolatilityFeatures] = []
        self.decisions: list[InventoryDecision] = []
        self.position_history: list[InventorySnapshot] = []
        self.equity_curve: list[EquitySnapshot] = []

    @property
    def name(self) -> str:
        return self.config.name

    def on_tick(self, state: MarketState) -> Signal:
        features = self._features.update(state)
        self.feature_history.append(features)
        side, reason = self._choose_side(state, features)
        if side == "none":
            signal = self._signal(
                side="none",
                reason=reason,
                confidence=0.0,
                target_notional=0.0,
                estimated_shares=0.0,
                features=features,
            )
            self._record_decision(state, signal, features)
            self._record_snapshots(state)
            return signal

        price = entry_price_for_signal(side, state.orderbook)
        if price is None or price <= 0:
            signal = self._signal(
                side="none",
                reason="missing contract ask for inventory leg",
                confidence=0.0,
                target_notional=0.0,
                estimated_shares=0.0,
                features=features,
            )
            self._record_decision(state, signal, features)
            self._record_snapshots(state)
            return signal

        target_notional = self._target_notional(features, reason)
        estimated_shares = target_notional / max(price, self.config.floor_price)
        confidence = min(0.95, self.config.min_confidence + features.volatility_regime_score * 0.03)
        signal = self._signal(
            side=side,
            reason=reason,
            confidence=confidence,
            target_notional=target_notional,
            estimated_shares=estimated_shares,
            features=features,
            allow_price_strike_mismatch=reason != "expansion momentum pyramid",
        )
        self._positions[side].add(qty=estimated_shares, price=price)
        self._record_decision(state, signal, features, entry_price=price)
        self._record_snapshots(state)
        return signal

    def _choose_side(self, state: MarketState, features: VolatilityFeatures) -> tuple[str, str]:
        if features.distance_from_strike_abs < self.config.min_distance_from_strike:
            return "none", "flat/low volatility near strike"
        if features.expansion_regime:
            return ("long_above" if features.distance_from_strike > 0 else "long_below"), "expansion momentum pyramid"
        if features.stabilization_regime:
            return ("long_below" if features.distance_from_strike > 0 else "long_above"), "stabilization crushed-side accumulation"
        if features.compression_regime and features.distance_from_strike_abs >= self.config.min_distance_from_strike:
            return ("long_below" if features.distance_from_strike > 0 else "long_above"), "late compression crushed-side accumulation"
        return "none", "flat/low volatility no inventory add"

    def _target_notional(self, features: VolatilityFeatures, reason: str) -> float:
        volatility = self._volatility_multiplier(features)
        distance = self._distance_multiplier(features)
        velocity = self._velocity_multiplier(features, reason)
        target = self.config.base_notional * volatility * distance * velocity
        return round(min(self.config.max_notional_per_add, max(0.0, target)), 6)

    def _volatility_multiplier(self, features: VolatilityFeatures) -> float:
        ratio = features.atr_1m / max(self.config.min_atr_1m, 1e-9)
        if ratio < 1.0:
            return self.config.low_atr_multiplier
        if ratio < 2.0:
            return self.config.normal_atr_multiplier
        if ratio < 4.0:
            return self.config.high_atr_multiplier
        return self.config.extreme_atr_multiplier

    def _distance_multiplier(self, features: VolatilityFeatures) -> float:
        if features.distance_from_strike_abs < 50.0:
            return self.config.near_distance_multiplier
        if features.distance_from_strike_abs < 200.0:
            return self.config.moderate_distance_multiplier
        return self.config.large_distance_multiplier

    def _velocity_multiplier(self, features: VolatilityFeatures, reason: str) -> float:
        if "stabilization" in reason or "compression" in reason:
            return self.config.slowdown_velocity_multiplier
        if features.velocity_away_from_strike <= 0:
            return self.config.flat_velocity_multiplier
        if features.velocity_away_from_strike >= self.config.expansion_velocity_threshold * 2.0:
            return self.config.fast_velocity_multiplier
        return self.config.normal_velocity_multiplier

    def _signal(
        self,
        *,
        side: str,
        reason: str,
        confidence: float,
        target_notional: float,
        estimated_shares: float,
        features: VolatilityFeatures,
        allow_price_strike_mismatch: bool = False,
    ) -> Signal:
        return Signal(
            side=side,
            reason=reason,
            confidence=confidence,
            strategy=self.name,
            target_notional=target_notional,
            estimated_shares=estimated_shares,
            features=dict(features.as_dict()),
            allow_price_strike_mismatch=allow_price_strike_mismatch,
        )

    def _record_decision(
        self,
        state: MarketState,
        signal: Signal,
        features: VolatilityFeatures,
        *,
        entry_price: float | None = None,
    ) -> None:
        self.decisions.append(
            InventoryDecision(
                ts=state.tick.ts.isoformat(),
                market_ticker=state.contract.ticker,
                side=signal.side,
                reason=signal.reason,
                target_notional=float(signal.target_notional or 0.0),
                estimated_shares=float(signal.estimated_shares or 0.0),
                entry_price=entry_price,
                features=features,
            )
        )

    def _record_snapshots(self, state: MarketState) -> None:
        above = self._positions["long_above"]
        below = self._positions["long_below"]
        smaller = min(above.qty, below.qty)
        larger = max(above.qty, below.qty)
        imbalance = larger / smaller if smaller > 0 else (larger if larger > 0 else 0.0)
        blended_basis = above.avg_price + below.avg_price if above.qty > 0 and below.qty > 0 else None
        ts = state.tick.ts.isoformat()
        self.position_history.append(
            InventorySnapshot(
                ts=ts,
                market_ticker=state.contract.ticker,
                above_qty=above.qty,
                above_avg_price=above.avg_price,
                below_qty=below.qty,
                below_avg_price=below.avg_price,
                blended_basis=blended_basis,
                imbalance_ratio=imbalance,
            )
        )
        yes_mark = state.orderbook.yes_bid
        no_mark = state.orderbook.no_bid
        mtm = (above.qty * (yes_mark or 0.0)) + (below.qty * (no_mark or 0.0))
        cost = above.notional + below.notional
        self.equity_curve.append(
            EquitySnapshot(
                ts=ts,
                market_ticker=state.contract.ticker,
                mtm_value=mtm,
                cost_basis=cost,
                unrealized_pnl=mtm - cost,
                above_mark=yes_mark,
                below_mark=no_mark,
            )
        )
