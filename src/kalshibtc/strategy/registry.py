from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .breakout_momentum import BreakoutMomentumStrategy
from .late_window_only import LateWindowOnlyStrategy
from .mean_reversion_to_strike import MeanReversionToStrikeStrategy
from .no_trade_baseline import NoTradeBaselineStrategy
from .signals import Strategy
from .simple_directional import SimpleDirectionalStrategy
from .simple_inventory_mm import SimpleInventoryMMConfig, SimpleInventoryMMStrategy
from .spread_aware_momentum import SpreadAwareMomentumStrategy
from .volatility_inventory import VolatilityInventoryStrategy

_FACTORIES: dict[str, Callable[[], Strategy]] = {
    "simple_directional": SimpleDirectionalStrategy,
    "simple_inventory_mm": SimpleInventoryMMStrategy,
    "mean_reversion_to_strike": MeanReversionToStrikeStrategy,
    "breakout_momentum": BreakoutMomentumStrategy,
    "late_window_only": LateWindowOnlyStrategy,
    "spread_aware_momentum": SpreadAwareMomentumStrategy,
    "no_trade_baseline": NoTradeBaselineStrategy,
    "volatility_inventory": VolatilityInventoryStrategy,
}


def strategy_names() -> tuple[str, ...]:
    return tuple(_FACTORIES)


def create_strategy(name: str, params: Mapping[str, Any] | None = None) -> Strategy:
    params = dict(params or {})
    try:
        factory = _FACTORIES[name]
    except KeyError as exc:
        expected = ", ".join(strategy_names())
        raise ValueError(f"unknown strategy: {name}; expected one of: {expected}") from exc
    if name == "simple_inventory_mm" and params:
        return SimpleInventoryMMStrategy(SimpleInventoryMMConfig(**params))
    if params:
        raise ValueError(f"strategy {name} does not accept CLI params")
    return factory()
