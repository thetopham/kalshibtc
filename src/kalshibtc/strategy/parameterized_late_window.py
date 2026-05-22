"""Research-only parameterized late-window strategy adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..market.pricing import entry_price_for_signal, spread_for_signal
from ..market.state import MarketState
from ..research.specs import CandidateSpec
from .signals import Signal


@dataclass(frozen=True)
class ParameterizedLateWindowStrategy:
    """Late-window directional candidate instantiated from a validated JSON spec."""

    name: str
    max_seconds_to_close: float = 60.0
    min_distance: float = 10.0
    max_entry_price: float = 0.90
    max_entry_spread: float = 0.05
    target_notional: float | None = None
    base_confidence: float = 0.65
    max_confidence: float = 0.95

    def on_tick(self, state: MarketState) -> Signal:
        seconds = float(state.seconds_to_close)
        if seconds > self.max_seconds_to_close:
            return Signal("none", "outside candidate entry window", 0.0, strategy=self.name)
        if seconds <= 0:
            return Signal("none", "market already closed", 0.0, strategy=self.name)

        distance = float(state.distance_from_strike)
        if abs(distance) < self.min_distance:
            return Signal("none", "candidate too close to strike", 0.0, strategy=self.name)

        side = "long_above" if distance > 0 else "long_below"
        entry_price = entry_price_for_signal(side, state.orderbook)
        if entry_price is None:
            return Signal("none", "missing candidate entry price", 0.0, strategy=self.name)
        if entry_price > self.max_entry_price:
            return Signal("none", "candidate entry price too expensive", 0.0, strategy=self.name)

        spread = spread_for_signal(side, state.orderbook)
        if spread is None:
            return Signal("none", "missing candidate spread", 0.0, strategy=self.name)
        if spread > self.max_entry_spread:
            return Signal("none", "candidate spread too wide", 0.0, strategy=self.name)

        distance_component = min(abs(distance), 200.0) / 400.0
        time_component = max(0.0, self.max_seconds_to_close - seconds) / 200.0
        confidence = min(self.max_confidence, self.base_confidence + distance_component + time_component)
        direction = "above" if side == "long_above" else "below"
        return Signal(
            side,
            f"candidate final-window {direction} strike with tradable book",
            confidence,
            strategy=self.name,
            target_notional=self.target_notional,
            features={
                "seconds_to_close": seconds,
                "distance_from_strike": distance,
                "entry_price": entry_price,
                "entry_spread": spread,
                "max_entry_price": self.max_entry_price,
                "max_entry_spread": self.max_entry_spread,
                "min_distance": self.min_distance,
            },
        )


def strategy_from_candidate_spec(spec: CandidateSpec) -> ParameterizedLateWindowStrategy:
    if spec.mechanism != "parameterized_late_window":
        raise ValueError(f"unsupported candidate mechanism: {spec.mechanism}")
    params = dict(spec.parameters)
    allowed = {
        "max_seconds_to_close",
        "min_distance",
        "max_entry_price",
        "max_entry_spread",
        "target_notional",
        "base_confidence",
        "max_confidence",
    }
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise ValueError(f"unknown parameterized_late_window parameter: {unknown[0]}")
    return ParameterizedLateWindowStrategy(
        name=f"candidate_{_safe_name(spec.name)}",
        max_seconds_to_close=_positive_float(params.get("max_seconds_to_close"), 60.0, "max_seconds_to_close"),
        min_distance=_nonnegative_float(params.get("min_distance"), 10.0, "min_distance"),
        max_entry_price=_positive_float(params.get("max_entry_price"), 0.90, "max_entry_price"),
        max_entry_spread=_nonnegative_float(params.get("max_entry_spread"), 0.05, "max_entry_spread"),
        target_notional=_optional_positive_float(params.get("target_notional"), "target_notional"),
        base_confidence=_nonnegative_float(params.get("base_confidence"), 0.65, "base_confidence"),
        max_confidence=_nonnegative_float(params.get("max_confidence"), 0.95, "max_confidence"),
    )


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", name.strip().lower()).strip("_")
    return cleaned or "unnamed"


def _positive_float(value: Any, default: float, name: str) -> float:
    parsed = _float(value, default)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _nonnegative_float(value: Any, default: float, name: str) -> float:
    parsed = _float(value, default)
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _optional_positive_float(value: Any, name: str) -> float | None:
    if value is None:
        return None
    parsed = _float(value, 0.0)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _float(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"expected numeric parameter, got {value!r}") from exc
