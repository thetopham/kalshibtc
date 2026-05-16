from __future__ import annotations

from collections.abc import Callable

from .breakout_momentum import BreakoutMomentumStrategy
from .late_window_only import LateWindowOnlyStrategy
from .mean_reversion_to_strike import MeanReversionToStrikeStrategy
from .no_trade_baseline import NoTradeBaselineStrategy
from .signals import Strategy
from .simple_directional import SimpleDirectionalStrategy
from .spread_aware_momentum import SpreadAwareMomentumStrategy

_FACTORIES: dict[str, Callable[[], Strategy]] = {
    "simple_directional": SimpleDirectionalStrategy,
    "mean_reversion_to_strike": MeanReversionToStrikeStrategy,
    "breakout_momentum": BreakoutMomentumStrategy,
    "late_window_only": LateWindowOnlyStrategy,
    "spread_aware_momentum": SpreadAwareMomentumStrategy,
    "no_trade_baseline": NoTradeBaselineStrategy,
}


def strategy_names() -> tuple[str, ...]:
    return tuple(_FACTORIES)


def create_strategy(name: str) -> Strategy:
    try:
        return _FACTORIES[name]()
    except KeyError as exc:
        expected = ", ".join(strategy_names())
        raise ValueError(f"unknown strategy: {name}; expected one of: {expected}") from exc
