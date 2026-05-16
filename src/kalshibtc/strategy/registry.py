from __future__ import annotations

from collections.abc import Callable

from .breakout_momentum import BreakoutMomentumStrategy
from .contrarian_spread_reversion import ContrarianSpreadReversionStrategy
from .inventory_vol_rebalance import InventoryVolRebalanceStrategy
from .inventory_vol_regime import InventoryVolRegimeStrategy
from .late_window_only import LateWindowOnlyStrategy
from .mean_reversion_to_strike import MeanReversionToStrikeStrategy
from .no_trade_baseline import NoTradeBaselineStrategy
from .pair_arb import PairArbStrategy
from .pair_arb_grid import PairArbGridStrategy
from .pair_arb_passive import PairArbPassiveStrategy
from .signals import Strategy
from .simple_directional import SimpleDirectionalStrategy
from .spread_aware_momentum import SpreadAwareMomentumStrategy
from .volatility_hedge import VolatilityHedgeStrategy

_FACTORIES: dict[str, Callable[[], Strategy]] = {
    "simple_directional": SimpleDirectionalStrategy,
    "mean_reversion_to_strike": MeanReversionToStrikeStrategy,
    "breakout_momentum": BreakoutMomentumStrategy,
    "late_window_only": LateWindowOnlyStrategy,
    "spread_aware_momentum": SpreadAwareMomentumStrategy,
    "contrarian_spread_reversion": ContrarianSpreadReversionStrategy,
    "pair_arb": PairArbStrategy,
    "pair_arb_grid": PairArbGridStrategy,
    "pair_arb_passive": PairArbPassiveStrategy,
    "inventory_vol_rebalance": InventoryVolRebalanceStrategy,
    "inventory_vol_regime": InventoryVolRegimeStrategy,
    "volatility_hedge": VolatilityHedgeStrategy,
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
