from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass

from ..market.state import MarketState


@dataclass(frozen=True)
class VolatilityFeatures:
    atr_1m: float
    atr_expansion_rate: float
    distance_from_strike: float
    distance_from_strike_abs: float
    velocity_away_from_strike: float
    velocity_slowdown: float
    macd_histogram: float
    macd_slope: float
    time_to_expiry_seconds: float
    volatility_regime_score: float
    expansion_regime: bool
    stabilization_regime: bool
    compression_regime: bool

    def as_dict(self) -> dict[str, float | bool]:
        return asdict(self)


@dataclass(frozen=True)
class VolatilityFeatureConfig:
    atr_window_seconds: float = 60.0
    baseline_window: int = 20
    fast_ema_period: int = 12
    slow_ema_period: int = 26
    signal_ema_period: int = 9
    min_atr_1m: float = 5.0
    expansion_velocity_threshold: float = 1.0
    slowdown_threshold: float = 1.0
    compression_seconds_to_expiry: float = 300.0


class VolatilityFeatureBuilder:
    """Incremental derived-feature layer for short-window BTC displacement regimes."""

    def __init__(
        self,
        *,
        atr_window_seconds: float = 60.0,
        baseline_window: int = 20,
        fast_ema_period: int = 12,
        slow_ema_period: int = 26,
        signal_ema_period: int = 9,
        min_atr_1m: float = 5.0,
        expansion_velocity_threshold: float = 1.0,
        slowdown_threshold: float = 1.0,
        compression_seconds_to_expiry: float = 300.0,
    ) -> None:
        self.config = VolatilityFeatureConfig(
            atr_window_seconds=atr_window_seconds,
            baseline_window=baseline_window,
            fast_ema_period=fast_ema_period,
            slow_ema_period=slow_ema_period,
            signal_ema_period=signal_ema_period,
            min_atr_1m=min_atr_1m,
            expansion_velocity_threshold=expansion_velocity_threshold,
            slowdown_threshold=slowdown_threshold,
            compression_seconds_to_expiry=compression_seconds_to_expiry,
        )
        self._samples: deque[tuple[float, float, float]] = deque()
        self._atr_history: deque[float] = deque(maxlen=max(2, baseline_window))
        self._last_macd_histogram = 0.0
        self._fast_ema: float | None = None
        self._slow_ema: float | None = None
        self._signal_ema: float | None = None

    def update(self, state: MarketState) -> VolatilityFeatures:
        ts = state.tick.ts.timestamp()
        price = state.price
        distance_abs = state.abs_distance_from_strike
        previous_distance_abs = self._samples[-1][2] if self._samples else distance_abs
        previous_ts = self._samples[-1][0] if self._samples else ts
        dt = max(1e-9, ts - previous_ts)
        velocity_away = (distance_abs - previous_distance_abs) / dt

        self._samples.append((ts, price, distance_abs))
        while self._samples and ts - self._samples[0][0] > self.config.atr_window_seconds:
            self._samples.popleft()

        atr = self._atr_from_samples()
        baseline = sum(self._atr_history) / len(self._atr_history) if self._atr_history else atr
        atr_expansion_rate = atr / baseline if baseline > 0 else (1.0 if atr == 0 else atr)
        self._atr_history.append(atr)

        macd_histogram = self._macd_histogram(price)
        macd_slope = macd_histogram - self._last_macd_histogram
        self._last_macd_histogram = macd_histogram

        velocity_slowdown = max(0.0, abs(velocity_away) if velocity_away < self.config.expansion_velocity_threshold else 0.0)
        time_to_expiry = state.seconds_to_close
        expansion = (
            atr >= self.config.min_atr_1m
            and velocity_away >= self.config.expansion_velocity_threshold
            and distance_abs > 0
        )
        stabilization = (
            atr >= self.config.min_atr_1m
            and velocity_slowdown >= self.config.slowdown_threshold
            and distance_abs > 0
        )
        compression = 0 <= time_to_expiry < self.config.compression_seconds_to_expiry
        score = min(10.0, (atr / max(self.config.min_atr_1m, 1e-9)) + max(0.0, velocity_away))
        return VolatilityFeatures(
            atr_1m=atr,
            atr_expansion_rate=atr_expansion_rate,
            distance_from_strike=state.distance_from_strike,
            distance_from_strike_abs=distance_abs,
            velocity_away_from_strike=velocity_away,
            velocity_slowdown=velocity_slowdown,
            macd_histogram=macd_histogram,
            macd_slope=macd_slope,
            time_to_expiry_seconds=time_to_expiry,
            volatility_regime_score=score,
            expansion_regime=expansion,
            stabilization_regime=stabilization,
            compression_regime=compression,
        )

    def _atr_from_samples(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        prices = [sample[1] for sample in self._samples]
        moves = [abs(prices[index] - prices[index - 1]) for index in range(1, len(prices))]
        return sum(moves) / len(moves) if moves else 0.0

    def _macd_histogram(self, price: float) -> float:
        self._fast_ema = _ema(self._fast_ema, price, self.config.fast_ema_period)
        self._slow_ema = _ema(self._slow_ema, price, self.config.slow_ema_period)
        macd = self._fast_ema - self._slow_ema
        self._signal_ema = _ema(self._signal_ema, macd, self.config.signal_ema_period)
        return macd - self._signal_ema


def _ema(previous: float | None, value: float, period: int) -> float:
    if previous is None:
        return value
    alpha = 2.0 / (period + 1.0)
    return value * alpha + previous * (1.0 - alpha)
