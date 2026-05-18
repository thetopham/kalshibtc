from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .breakout_momentum import BreakoutMomentumStrategy
from .cheap_accumulate_repair_v0 import CheapAccumulateRepairConfig, CheapAccumulateRepairV0Strategy
from .complement_ladder_v0 import ComplementLadderConfig, ComplementLadderV0Strategy
from .late_window_only import LateWindowOnlyStrategy
from .mean_reversion_to_strike import MeanReversionToStrikeStrategy
from .no_trade_baseline import NoTradeBaselineStrategy
from .seed_cheap_accumulate_repair_v1 import SeedCheapAccumulateRepairConfig, SeedCheapAccumulateRepairV1Strategy
from .seed_cheap_accumulate_repair_v2 import SeedCheapAccumulateRepairV2Config, SeedCheapAccumulateRepairV2Strategy
from .signals import Strategy
from .simple_directional import SimpleDirectionalStrategy
from .simple_inventory_mm import SimpleInventoryMMConfig, SimpleInventoryMMStrategy
from .spread_aware_momentum import SpreadAwareMomentumStrategy
from .strategy_probability_mm_v0 import StrategyProbabilityMMV0Config, StrategyProbabilityMMV0Strategy
from .volatility_inventory import VolatilityInventoryStrategy

_FACTORIES: dict[str, Callable[[], Strategy]] = {
    "simple_directional": SimpleDirectionalStrategy,
    "simple_inventory_mm": SimpleInventoryMMStrategy,
    "complement_ladder_v0": ComplementLadderV0Strategy,
    "cheap_accumulate_repair_v0": CheapAccumulateRepairV0Strategy,
    "seed_cheap_accumulate_repair_v1": SeedCheapAccumulateRepairV1Strategy,
    "seed_cheap_accumulate_repair_v2": SeedCheapAccumulateRepairV2Strategy,
    "mean_reversion_to_strike": MeanReversionToStrikeStrategy,
    "breakout_momentum": BreakoutMomentumStrategy,
    "late_window_only": LateWindowOnlyStrategy,
    "spread_aware_momentum": SpreadAwareMomentumStrategy,
    "no_trade_baseline": NoTradeBaselineStrategy,
    "volatility_inventory": VolatilityInventoryStrategy,
    "strategy_probability_mm_v0": StrategyProbabilityMMV0Strategy,
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
    if name == "complement_ladder_v0" and params:
        return ComplementLadderV0Strategy(ComplementLadderConfig(**params))
    if name == "cheap_accumulate_repair_v0" and params:
        return CheapAccumulateRepairV0Strategy(CheapAccumulateRepairConfig(**params))
    if name == "seed_cheap_accumulate_repair_v1" and params:
        return SeedCheapAccumulateRepairV1Strategy(SeedCheapAccumulateRepairConfig(**params))
    if name == "seed_cheap_accumulate_repair_v2" and params:
        return SeedCheapAccumulateRepairV2Strategy(SeedCheapAccumulateRepairV2Config(**params))
    if name == "strategy_probability_mm_v0" and params:
        return StrategyProbabilityMMV0Strategy(StrategyProbabilityMMV0Config(**params))
    if params:
        raise ValueError(f"strategy {name} does not accept CLI params")
    return factory()
