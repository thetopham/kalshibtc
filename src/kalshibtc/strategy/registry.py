from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .bayesian_markov_directional import (
    BayesianMarkovDirectionalConfig,
    BayesianMarkovDirectionalStrategy,
)
from .breakout_momentum import BreakoutMomentumStrategy
from .cheap_accumulate_repair_v0 import CheapAccumulateRepairConfig, CheapAccumulateRepairV0Strategy
from .complement_ladder_v0 import ComplementLadderConfig, ComplementLadderV0Strategy
from .contrarian_spread_reversion import ContrarianSpreadReversionStrategy
from .hedge_volatility_v0 import HedgeVolatilityV0
from .inventory_aware_passive_mm import (
    InventoryAwarePassiveMMConfig,
    InventoryAwarePassiveMMStrategy,
)
from .inventory_vol_rebalance import InventoryVolRebalanceStrategy
from .inventory_vol_regime import InventoryVolRegimeStrategy
from .late_window_only import LateWindowOnlyStrategy
from .late_lotto_ticket import LateLottoTicketConfig, LateLottoTicketStrategy
from .mean_reversion_to_strike import MeanReversionToStrikeStrategy
from .no_trade_baseline import NoTradeBaselineStrategy
from .pair_arb import PairArbStrategy
from .pair_arb_grid import PairArbGridStrategy
from .pair_arb_passive import PairArbPassiveStrategy
from .seed_cheap_accumulate_repair_v1 import (
    SeedCheapAccumulateRepairConfig,
    SeedCheapAccumulateRepairV1Strategy,
)
from .seed_cheap_accumulate_repair_v2 import (
    SeedCheapAccumulateRepairV2Config,
    SeedCheapAccumulateRepairV2Strategy,
)
from .simple_directional import SimpleDirectionalStrategy
from .simple_inventory_mm import SimpleInventoryMMConfig, SimpleInventoryMMStrategy
from .spread_aware_momentum import SpreadAwareMomentumStrategy
from .strategy_probability_mm_v0 import (
    StrategyProbabilityMMV0Config,
    StrategyProbabilityMMV0Strategy,
)
from .volatility_hedge import VolatilityHedgeStrategy
from .volatility_inventory import VolatilityInventoryStrategy


@dataclass(frozen=True)
class StrategyMetadata:
    name: str
    allowed_venues: tuple[str, ...] = ("kalshi", "polymarket")
    hedges_inventory: bool = False

    @property
    def polymarket_only(self) -> bool:
        return self.allowed_venues == ("polymarket",)


POLYMARKET_ONLY_HEDGING_STRATEGIES: frozenset[str] = frozenset(
    {
        "hedge_volatility_v0",
        "volatility_hedge",
        "contrarian_spread_reversion",
        "complement_ladder_v0",
        "cheap_accumulate_repair_v0",
        "seed_cheap_accumulate_repair_v1",
        "seed_cheap_accumulate_repair_v2",
        "inventory_aware_passive_mm",
    }
)

KALSHI_ONLY_DIRECTIONAL_STRATEGIES: frozenset[str] = frozenset(
    {
        "strategy_probability_mm_v0",
        "bayesian_markov_directional",
        "late_lotto_ticket",
    }
)

_FACTORIES: dict[str, Callable[[], Any]] = {
    "simple_directional": SimpleDirectionalStrategy,
    "simple_inventory_mm": SimpleInventoryMMStrategy,
    "complement_ladder_v0": ComplementLadderV0Strategy,
    "cheap_accumulate_repair_v0": CheapAccumulateRepairV0Strategy,
    "seed_cheap_accumulate_repair_v1": SeedCheapAccumulateRepairV1Strategy,
    "seed_cheap_accumulate_repair_v2": SeedCheapAccumulateRepairV2Strategy,
    "mean_reversion_to_strike": MeanReversionToStrikeStrategy,
    "breakout_momentum": BreakoutMomentumStrategy,
    "late_window_only": LateWindowOnlyStrategy,
    "late_lotto_ticket": LateLottoTicketStrategy,
    "spread_aware_momentum": SpreadAwareMomentumStrategy,
    "contrarian_spread_reversion": ContrarianSpreadReversionStrategy,
    "pair_arb": PairArbStrategy,
    "pair_arb_grid": PairArbGridStrategy,
    "pair_arb_passive": PairArbPassiveStrategy,
    "inventory_vol_rebalance": InventoryVolRebalanceStrategy,
    "inventory_vol_regime": InventoryVolRegimeStrategy,
    "volatility_hedge": VolatilityHedgeStrategy,
    "no_trade_baseline": NoTradeBaselineStrategy,
    "volatility_inventory": VolatilityInventoryStrategy,
    "strategy_probability_mm_v0": StrategyProbabilityMMV0Strategy,
    "bayesian_markov_directional": BayesianMarkovDirectionalStrategy,
    "inventory_aware_passive_mm": InventoryAwarePassiveMMStrategy,
    "hedge_volatility_v0": HedgeVolatilityV0,
}


def _allowed_venues_for_strategy(name: str) -> tuple[str, ...]:
    if name in POLYMARKET_ONLY_HEDGING_STRATEGIES:
        return ("polymarket",)
    if name in KALSHI_ONLY_DIRECTIONAL_STRATEGIES:
        return ("kalshi",)
    return ("kalshi", "polymarket")


_METADATA: dict[str, StrategyMetadata] = {
    name: StrategyMetadata(
        name=name,
        allowed_venues=_allowed_venues_for_strategy(name),
        hedges_inventory=name in POLYMARKET_ONLY_HEDGING_STRATEGIES,
    )
    for name in _FACTORIES
}


def strategy_names() -> tuple[str, ...]:
    return tuple(_FACTORIES)


def strategy_metadata(name: str) -> StrategyMetadata:
    try:
        return _METADATA[name]
    except KeyError as exc:
        expected = ", ".join(strategy_names())
        raise ValueError(f"unknown strategy: {name}; expected one of: {expected}") from exc


def strategies_for_venue(venue: str) -> tuple[str, ...]:
    normalized = venue.lower()
    if normalized not in {"kalshi", "polymarket"}:
        raise ValueError("venue must be kalshi or polymarket")
    return tuple(name for name, metadata in _METADATA.items() if normalized in metadata.allowed_venues)


def create_strategy(name: str, params: Mapping[str, Any] | None = None, *, venue: str | None = None) -> Any:
    params = dict(params or {})
    try:
        factory = _FACTORIES[name]
    except KeyError as exc:
        expected = ", ".join(strategy_names())
        raise ValueError(f"unknown strategy: {name}; expected one of: {expected}") from exc
    if venue is not None:
        normalized_venue = venue.lower()
        metadata = strategy_metadata(name)
        if normalized_venue not in metadata.allowed_venues:
            allowed = ", ".join(metadata.allowed_venues)
            raise ValueError(f"strategy {name} is not enabled for venue {normalized_venue}; allowed venues: {allowed}")
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
    if name == "strategy_probability_mm_v0":
        if venue is not None:
            params.setdefault("venue", venue.lower())
        if params:
            return StrategyProbabilityMMV0Strategy(StrategyProbabilityMMV0Config(**params))
        return StrategyProbabilityMMV0Strategy(StrategyProbabilityMMV0Config(venue=venue.lower() if venue is not None else "kalshi"))
    if name == "bayesian_markov_directional":
        return BayesianMarkovDirectionalStrategy(BayesianMarkovDirectionalConfig(**params))
    if name == "late_lotto_ticket":
        return LateLottoTicketStrategy(LateLottoTicketConfig(**params))
    if name == "inventory_aware_passive_mm" and params:
        return InventoryAwarePassiveMMStrategy(InventoryAwarePassiveMMConfig(**params))
    if params:
        raise ValueError(f"strategy {name} does not accept CLI params")
    return factory()
