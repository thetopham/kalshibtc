from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


def compute_metrics(
    fills: Sequence[Mapping[str, Any] | Any],
    *,
    annualization: int = 252,
    initial_capital: float = 100.0,
) -> dict[str, float | int]:
    """Compute replay/paper-fill metrics for strategy research.

    A fill may be either a mapping or a dataclass-like object. If no explicit
    ``pnl`` exists, replay fills are treated as flat/unsettled and contribute
    zero PnL while still counting notional/trades. Returns are approximated as
    pnl / initial_capital so the institutional metrics are stable for local
    replay comparisons until official settlement PnL is available.
    """
    pnls = [_as_float(_field(fill, "pnl"), 0.0) for fill in fills]
    notionals = [_as_float(_field(fill, "notional"), 0.0) for fill in fills]
    trades = len(pnls)
    wins = sum(1 for pnl in pnls if pnl > 0)
    losses = sum(1 for pnl in pnls if pnl < 0)
    total_pnl = sum(pnls)
    gross_profit = sum(pnl for pnl in pnls if pnl > 0)
    gross_loss = sum(pnl for pnl in pnls if pnl < 0)
    avg_win = gross_profit / wins if wins else 0.0
    avg_loss = gross_loss / losses if losses else 0.0
    largest_win = max((pnl for pnl in pnls if pnl > 0), default=0.0)
    largest_loss = min((pnl for pnl in pnls if pnl < 0), default=0.0)

    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    returns = [pnl / initial_capital for pnl in pnls] if initial_capital else []
    mean_return = _mean(returns)
    volatility = _sample_std(returns) * math.sqrt(annualization) if len(returns) >= 2 else 0.0
    sharpe = _annualized_ratio(returns, annualization)
    downside = [ret for ret in returns if ret < 0]
    sortino = _annualized_ratio(downside, annualization, numerator_mean=mean_return)
    max_drawdown_pct = max_drawdown / initial_capital if initial_capital else 0.0
    mean_return_ann = mean_return * annualization
    calmar = mean_return_ann / max_drawdown_pct if max_drawdown_pct > 0 else 0.0
    profit_factor = abs(gross_profit / gross_loss) if gross_loss < 0 else (math.inf if gross_profit > 0 else 0.0)

    return {
        "trades": trades,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / trades if trades else 0.0,
        "total_pnl": total_pnl,
        "ev_per_trade": total_pnl / trades if trades else 0.0,
        "max_drawdown": max_drawdown,
        "max_drawdown_pct": max_drawdown_pct,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": calmar,
        "volatility": volatility,
        "mean_return": mean_return,
        "skewness": _skewness(returns),
        "excess_kurtosis": _excess_kurtosis(returns),
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "largest_win": largest_win,
        "largest_loss": largest_loss,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "notional": sum(notionals),
    }


def _field(fill: Mapping[str, Any] | Any, name: str) -> Any:
    if isinstance(fill, Mapping):
        return fill.get(name)
    return getattr(fill, name, None)


def _as_float(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _sample_std(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def _population_std(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    mean = _mean(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return math.sqrt(variance)


def _annualized_ratio(
    values: Sequence[float],
    annualization: int,
    *,
    numerator_mean: float | None = None,
) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values) if numerator_mean is None else numerator_mean
    std = _sample_std(values)
    if std == 0:
        return 0.0
    return mean / std * math.sqrt(annualization)


def _skewness(values: Sequence[float]) -> float:
    if len(values) < 3:
        return 0.0
    mean = _mean(values)
    std = _population_std(values)
    if std == 0:
        return 0.0
    return sum(((value - mean) / std) ** 3 for value in values) / len(values)


def _excess_kurtosis(values: Sequence[float]) -> float:
    if len(values) < 4:
        return 0.0
    mean = _mean(values)
    std = _population_std(values)
    if std == 0:
        return 0.0
    kurtosis = sum(((value - mean) / std) ** 4 for value in values) / len(values)
    return kurtosis - 3.0
