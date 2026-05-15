from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def compute_metrics(fills: Sequence[Mapping[str, Any]]) -> dict[str, float | int]:
    pnls = [float(fill.get("pnl", 0.0) or 0.0) for fill in fills]
    trades = len(pnls)
    wins = sum(1 for pnl in pnls if pnl > 0)
    total_pnl = sum(pnls)
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "trades": trades,
        "wins": wins,
        "losses": sum(1 for pnl in pnls if pnl < 0),
        "win_rate": wins / trades if trades else 0.0,
        "total_pnl": total_pnl,
        "ev_per_trade": total_pnl / trades if trades else 0.0,
        "max_drawdown": max_drawdown,
    }
