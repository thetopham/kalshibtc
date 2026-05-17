from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

SETTLEMENT_SOURCE_REPLAY_FINAL_SNAPSHOT = "replay_final_snapshot"
SETTLEMENT_SOURCE_KALSHI_API = "kalshi_api"


def estimate_replay_fill_pnls(
    fills: Sequence[Mapping[str, Any] | Any],
    settlement_rows: Sequence[Mapping[str, Any] | Any],
) -> list[dict[str, Any]]:
    """Estimate replay fill PnL from the final recorded snapshot per market.

    This is an offline replay metric only. It does not call Kalshi and it does
    not claim official settlement. For production/paper accounting, official
    Kalshi outcomes remain preferred; this function is just enough to make
    research runs compare strategies with a deterministic end-of-tape outcome.
    """
    outcomes = _settlement_outcomes(settlement_rows)
    settled: list[dict[str, Any]] = []
    for fill in fills:
        market_ticker = str(_field(fill, "market_ticker") or "")
        settlement = outcomes.get(market_ticker)
        item = _fill_to_dict(fill)
        if settlement is None:
            item.update(
                {
                    "pnl": 0.0,
                    "settlement_result": None,
                    "settlement_source": "unsettled_no_snapshot",
                    "exit_price": None,
                }
            )
            settled.append(item)
            continue

        side = str(_field(fill, "side") or "").lower()
        outcome = settlement["outcome"]
        won = (side in {"long_above", "yes", "buy_yes"} and outcome == "above") or (
            side in {"long_below", "no", "buy_no"} and outcome == "below"
        )
        exit_price = 1.0 if won else 0.0
        contracts = _as_float(_field(fill, "contracts"), 0.0)
        notional = _as_float(_field(fill, "notional"), 0.0)
        item.update(
            {
                "pnl": contracts * exit_price - notional,
                "settlement_result": outcome,
                "settlement_source": settlement["source"],
                "exit_price": exit_price,
            }
        )
        settled.append(item)
    return settled


def compute_portfolio_settlement(
    fills: Sequence[Mapping[str, Any] | Any],
    settlement_rows: Sequence[Mapping[str, Any] | Any],
) -> dict[str, Any]:
    """Settle replay fills as per-market YES/NO inventory portfolios."""
    outcomes = _settlement_outcomes(settlement_rows)
    by_market: dict[str, dict[str, Any]] = {}
    for fill in fills:
        market_ticker = str(_field(fill, "market_ticker") or "")
        if not market_ticker:
            continue
        row = by_market.setdefault(
            market_ticker,
            {"market_ticker": market_ticker, "yes_contracts": 0.0, "no_contracts": 0.0, "yes_cost": 0.0, "no_cost": 0.0},
        )
        side = str(_field(fill, "side") or "").lower()
        contracts = _as_float(_field(fill, "contracts"), 0.0)
        notional = _as_float(_field(fill, "notional"), 0.0)
        if side in {"long_above", "yes", "buy_yes"}:
            row["yes_contracts"] += contracts
            row["yes_cost"] += notional
        elif side in {"long_below", "no", "buy_no"}:
            row["no_contracts"] += contracts
            row["no_cost"] += notional

    markets: list[dict[str, Any]] = []
    for market_ticker, row in sorted(by_market.items()):
        settlement = outcomes.get(market_ticker)
        outcome = settlement["outcome"] if settlement else None
        source = settlement["source"] if settlement else "unsettled_no_snapshot"
        yes_qty = float(row["yes_contracts"])
        no_qty = float(row["no_contracts"])
        yes_cost = float(row["yes_cost"])
        no_cost = float(row["no_cost"])
        total_cost = yes_cost + no_cost
        avg_yes = yes_cost / yes_qty if yes_qty > 0 else None
        avg_no = no_cost / no_qty if no_qty > 0 else None
        payout = yes_qty if outcome == "above" else no_qty if outcome == "below" else 0.0
        paired_qty = min(yes_qty, no_qty)
        paired_cost = (avg_yes or 0.0) + (avg_no or 0.0) if yes_qty > 0 and no_qty > 0 else None
        paired_locked_edge = paired_qty * (1.0 - paired_cost) if paired_cost is not None else 0.0
        raw_net_contracts = yes_qty - no_qty
        realized_pnl = payout - total_cost
        markets.append(
            {
                "market_ticker": market_ticker,
                "settlement_result": outcome,
                "settlement_source": source,
                "yes_contracts": yes_qty,
                "no_contracts": no_qty,
                "avg_yes_entry": avg_yes,
                "avg_no_entry": avg_no,
                "yes_cost": yes_cost,
                "no_cost": no_cost,
                "total_cost": total_cost,
                "gross_payout": payout,
                "realized_pnl": realized_pnl,
                "paired_contracts": paired_qty,
                "paired_cost": paired_cost,
                "paired_locked_edge": paired_locked_edge,
                "raw_net_contracts": raw_net_contracts,
                "final_unpaired_yes_contracts": max(0.0, raw_net_contracts),
                "final_unpaired_no_contracts": max(0.0, -raw_net_contracts),
            }
        )

    settled_markets = [m for m in markets if m["settlement_source"] != "unsettled_no_snapshot"]
    metric_markets = [m for m in settled_markets if m["settlement_source"] == SETTLEMENT_SOURCE_KALSHI_API] or settled_markets
    aggregate = {
        "markets": len(markets),
        "settled_markets": len(settled_markets),
        "metric_markets": len(metric_markets),
        "total_cost": sum(float(m["total_cost"]) for m in metric_markets),
        "gross_payout": sum(float(m["gross_payout"]) for m in metric_markets),
        "realized_pnl": sum(float(m["realized_pnl"]) for m in metric_markets),
        "paired_locked_edge": sum(float(m["paired_locked_edge"]) for m in metric_markets),
        "final_unpaired_yes_contracts": sum(float(m["final_unpaired_yes_contracts"]) for m in metric_markets),
        "final_unpaired_no_contracts": sum(float(m["final_unpaired_no_contracts"]) for m in metric_markets),
        "max_abs_raw_net_contracts": max((abs(float(m["raw_net_contracts"])) for m in metric_markets), default=0.0),
        "settlement_sources": sorted({str(m["settlement_source"]) for m in markets if m.get("settlement_source")}),
    }
    return {"aggregate": aggregate, "markets": markets}


def _settlement_outcomes(rows: Sequence[Mapping[str, Any] | Any]) -> dict[str, dict[str, str]]:
    outcomes: dict[str, dict[str, str]] = {}
    for row in rows:
        market_ticker = str(_field(row, "market_ticker") or "")
        if not market_ticker:
            continue
        source = str(_field(row, "source") or "")
        status = str(_field(row, "status") or "")
        winning_side = str(_field(row, "winning_side") or "").lower()
        if source == SETTLEMENT_SOURCE_KALSHI_API and status == "settled_official" and winning_side in {"yes", "no"}:
            outcomes[market_ticker] = {
                "outcome": "above" if winning_side == "yes" else "below",
                "source": SETTLEMENT_SOURCE_KALSHI_API,
            }
            continue

        price = _as_float(_first_present(row, "btc_price", "price", "settlement_price"), 0.0)
        strike = _as_float(_first_present(row, "target_price", "strike"), 0.0)
        if price > strike:
            outcomes[market_ticker] = {"outcome": "above", "source": SETTLEMENT_SOURCE_REPLAY_FINAL_SNAPSHOT}
        elif price < strike:
            outcomes[market_ticker] = {"outcome": "below", "source": SETTLEMENT_SOURCE_REPLAY_FINAL_SNAPSHOT}
        else:
            outcomes[market_ticker] = {"outcome": "at", "source": SETTLEMENT_SOURCE_REPLAY_FINAL_SNAPSHOT}
    return outcomes


def _fill_to_dict(fill: Mapping[str, Any] | Any) -> dict[str, Any]:
    keys = ("strategy", "market_ticker", "side", "entry_price", "notional", "contracts", "ts", "mode")
    data: dict[str, Any] = {}
    for key in keys:
        value = _field(fill, key)
        if value is not None:
            data[key] = value
    return data


def _first_present(row: Mapping[str, Any] | Any, *keys: str) -> Any:
    for key in keys:
        value = _field(row, key)
        if value is not None:
            return value
    return None


def _field(item: Mapping[str, Any] | Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    try:
        return item[name]  # type: ignore[index]
    except (TypeError, KeyError, IndexError):
        return getattr(item, name, None)


def _as_float(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
