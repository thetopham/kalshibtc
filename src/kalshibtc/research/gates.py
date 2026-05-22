"""Deterministic validation gates for replay-only research candidates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ResearchGateThresholds:
    """Serializable thresholds for rejecting weak research candidates."""

    min_trades: int = 10
    require_positive_pnl: bool = True
    max_drawdown: float | None = None
    min_sharpe: float | None = None
    min_sharpe_trades: int = 30

    def to_dict(self) -> dict[str, int | float | bool | None]:
        return asdict(self)


def evaluate_research_gates(
    metrics: dict[str, Any],
    thresholds: ResearchGateThresholds | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate deterministic replay/research gates.

    Returns a JSON-serializable payload so failed candidates can still be saved
    with explicit reasons and thresholds. Missing metrics fail safe as zeros.
    """

    gate_thresholds = _coerce_thresholds(thresholds)
    trades = _as_int(metrics.get("trades"), 0)
    total_pnl = _as_float(metrics.get("total_pnl"), 0.0)
    max_drawdown = _as_float(metrics.get("max_drawdown"), 0.0)
    sharpe = _as_float(metrics.get("sharpe"), 0.0)

    failed_reasons: list[str] = []
    if trades < gate_thresholds.min_trades:
        failed_reasons.append(f"trades {trades} below minimum {gate_thresholds.min_trades}")
    if gate_thresholds.require_positive_pnl and total_pnl <= 0:
        failed_reasons.append(f"total_pnl {_number_text(total_pnl)} is not positive")
    if gate_thresholds.max_drawdown is not None and max_drawdown > gate_thresholds.max_drawdown:
        failed_reasons.append(
            f"max_drawdown {_number_text(max_drawdown)} exceeds cap {_number_text(gate_thresholds.max_drawdown)}"
        )
    if (
        gate_thresholds.min_sharpe is not None
        and trades >= gate_thresholds.min_sharpe_trades
        and sharpe < gate_thresholds.min_sharpe
    ):
        failed_reasons.append(
            f"sharpe {_number_text(sharpe)} below minimum {_number_text(gate_thresholds.min_sharpe)}"
        )

    return {
        "passed": not failed_reasons,
        "failed_reasons": failed_reasons,
        "thresholds": gate_thresholds.to_dict(),
        "metrics": {
            "trades": trades,
            "total_pnl": total_pnl,
            "max_drawdown": max_drawdown,
            "sharpe": sharpe,
        },
    }


def _coerce_thresholds(thresholds: ResearchGateThresholds | dict[str, Any] | None) -> ResearchGateThresholds:
    if thresholds is None:
        return ResearchGateThresholds()
    if isinstance(thresholds, ResearchGateThresholds):
        return thresholds
    return ResearchGateThresholds(**thresholds)


def _as_float(value: Any, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _number_text(value: float) -> str:
    return str(float(value))
