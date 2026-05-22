from __future__ import annotations

import re
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from datetime import datetime
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
    *,
    position_mode: str = "portfolio",
) -> dict[str, Any]:
    """Settle replay fills as per-market portfolios or Kalshi position flips."""
    if position_mode not in {"portfolio", "kalshi_single_position"}:
        raise ValueError("position_mode must be 'portfolio' or 'kalshi_single_position'")
    outcomes = _settlement_outcomes(settlement_rows)
    if position_mode == "kalshi_single_position":
        fills = _kalshi_single_position_fills(fills)
    by_market: dict[str, dict[str, Any]] = {}
    for fill in fills:
        market_ticker = str(_field(fill, "market_ticker") or "")
        if not market_ticker:
            continue
        row = by_market.setdefault(
            market_ticker,
            {"market_ticker": market_ticker, "yes_contracts": 0.0, "no_contracts": 0.0, "yes_cost": 0.0, "no_cost": 0.0, "fills": []},
        )
        side = str(_field(fill, "side") or "").lower()
        contracts = _as_float(_field(fill, "contracts"), 0.0)
        notional = _as_float(_field(fill, "notional"), 0.0)
        row["fills"].append(fill)
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
        date = settlement.get("date") if settlement else _market_date(market_ticker)
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
        paired_split = _paired_unpaired_split_for_market(
            market_ticker=market_ticker,
            fills=row.get("fills", []),
            outcome=outcome,
            source=source,
        )
        markets.append(
            {
                "market_ticker": market_ticker,
                "date": date,
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
                "completed_pair_cost": paired_split["completed_pair_cost"],
                "completed_pair_payout": paired_split["completed_pair_payout"],
                "completed_pair_pnl": paired_split["completed_pair_pnl"],
                "completed_pair_contracts": paired_split["completed_pair_contracts"],
                "unpaired_leftover_cost": paired_split["unpaired_leftover_cost"],
                "unpaired_leftover_payout": paired_split["unpaired_leftover_payout"],
                "unpaired_leftover_pnl": paired_split["unpaired_leftover_pnl"],
                "unpaired_yes_cost": paired_split["unpaired_yes_cost"],
                "unpaired_yes_pnl": paired_split["unpaired_yes_pnl"],
                "unpaired_no_cost": paired_split["unpaired_no_cost"],
                "unpaired_no_pnl": paired_split["unpaired_no_pnl"],
                "unpaired_time_buckets": paired_split["unpaired_time_buckets"],
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
        "pnl_split": _aggregate_pnl_split(metric_markets),
    }
    return {"aggregate": aggregate, "markets": markets, "daily": _daily_portfolio_metrics(markets)}


def _kalshi_single_position_fills(fills: Sequence[Mapping[str, Any] | Any]) -> list[dict[str, Any]]:
    """Normalize fills so each Kalshi market has at most one open side.

    Kalshi BTC up/down exposure is a single position: buying the opposite side
    exits/flips the current position rather than creating simultaneous YES+NO
    inventory. For settlement accounting, only the latest open position per
    market should remain at expiry.
    """
    current: dict[str, dict[str, Any]] = {}
    for fill in fills:
        market_ticker = str(_field(fill, "market_ticker") or "")
        if not market_ticker:
            continue
        side = _normalized_side(_field(fill, "side"))
        if side is None:
            continue
        row = _fill_to_dict(fill)
        row["side"] = "long_above" if side == "yes" else "long_below"
        current[market_ticker] = row
    return [current[key] for key in sorted(current)]


def _normalized_side(side: object) -> str | None:
    value = str(side or "").lower()
    if value in {"long_above", "yes", "buy_yes"}:
        return "yes"
    if value in {"long_below", "no", "buy_no"}:
        return "no"
    return None


def _daily_portfolio_metrics(markets: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for market in markets:
        grouped[str(market.get("date") or _market_date(str(market.get("market_ticker") or "")))].append(market)
    rows: list[dict[str, Any]] = []
    for date, group in sorted(grouped.items()):
        metric_markets = [m for m in group if m.get("settlement_source") == SETTLEMENT_SOURCE_KALSHI_API]
        if not metric_markets:
            metric_markets = [m for m in group if m.get("settlement_source") != "unsettled_no_snapshot"]
        rows.append(
            {
                "date": date,
                "contracts": len(group),
                "settled_contracts": len(metric_markets),
                "total_cost": sum(float(m["total_cost"]) for m in metric_markets),
                "gross_payout": sum(float(m["gross_payout"]) for m in metric_markets),
                "realized_pnl": sum(float(m["realized_pnl"]) for m in metric_markets),
                "paired_locked_edge": sum(float(m["paired_locked_edge"]) for m in metric_markets),
                "final_unpaired_yes_contracts": sum(float(m["final_unpaired_yes_contracts"]) for m in metric_markets),
                "final_unpaired_no_contracts": sum(float(m["final_unpaired_no_contracts"]) for m in metric_markets),
                "max_abs_raw_net_contracts": max((abs(float(m["raw_net_contracts"])) for m in metric_markets), default=0.0),
                "settlement_sources": sorted({str(m["settlement_source"]) for m in group if m.get("settlement_source")}),
                "pnl_split": _aggregate_pnl_split(metric_markets),
            }
        )
    return rows


def _paired_unpaired_split_for_market(
    *,
    market_ticker: str,
    fills: Sequence[Mapping[str, Any] | Any],
    outcome: str | None,
    source: str,
) -> dict[str, Any]:
    yes_lots: deque[dict[str, Any]] = deque()
    no_lots: deque[dict[str, Any]] = deque()
    for fill in fills:
        side = str(_field(fill, "side") or "").lower()
        contracts = _as_float(_field(fill, "contracts"), 0.0)
        notional = _as_float(_field(fill, "notional"), 0.0)
        if contracts <= 0:
            continue
        price = notional / contracts if contracts > 0 else 0.0
        lot = {
            "side": "yes" if side in {"long_above", "yes", "buy_yes"} else "no" if side in {"long_below", "no", "buy_no"} else "",
            "contracts": contracts,
            "price": price,
            "cost": notional,
            "ts": _field(fill, "ts"),
        }
        if lot["side"] == "yes":
            yes_lots.append(lot)
        elif lot["side"] == "no":
            no_lots.append(lot)

    completed_pair_contracts = 0.0
    completed_pair_cost = 0.0
    completed_pair_payout = 0.0
    while yes_lots and no_lots:
        yes = yes_lots[0]
        no = no_lots[0]
        qty = min(float(yes["contracts"]), float(no["contracts"]))
        if qty <= 0:
            break
        yes_cost = qty * float(yes["price"])
        no_cost = qty * float(no["price"])
        completed_pair_contracts += qty
        completed_pair_cost += yes_cost + no_cost
        completed_pair_payout += qty if outcome in {"above", "below"} else 0.0
        yes["contracts"] = float(yes["contracts"]) - qty
        yes["cost"] = float(yes["contracts"]) * float(yes["price"])
        no["contracts"] = float(no["contracts"]) - qty
        no["cost"] = float(no["contracts"]) * float(no["price"])
        if float(yes["contracts"]) <= 1e-9:
            yes_lots.popleft()
        if float(no["contracts"]) <= 1e-9:
            no_lots.popleft()

    unpaired_yes_cost = sum(float(lot["cost"]) for lot in yes_lots)
    unpaired_no_cost = sum(float(lot["cost"]) for lot in no_lots)
    unpaired_yes_payout = sum(float(lot["contracts"]) for lot in yes_lots) if outcome == "above" else 0.0
    unpaired_no_payout = sum(float(lot["contracts"]) for lot in no_lots) if outcome == "below" else 0.0
    unpaired_buckets = _unpaired_time_buckets([*yes_lots, *no_lots], outcome=outcome)
    completed_pair_pnl = completed_pair_payout - completed_pair_cost
    unpaired_leftover_cost = unpaired_yes_cost + unpaired_no_cost
    unpaired_leftover_payout = unpaired_yes_payout + unpaired_no_payout
    return {
        "market_ticker": market_ticker,
        "settlement_source": source,
        "completed_pair_contracts": completed_pair_contracts,
        "completed_pair_cost": completed_pair_cost,
        "completed_pair_payout": completed_pair_payout,
        "completed_pair_pnl": completed_pair_pnl,
        "unpaired_leftover_cost": unpaired_leftover_cost,
        "unpaired_leftover_payout": unpaired_leftover_payout,
        "unpaired_leftover_pnl": unpaired_leftover_payout - unpaired_leftover_cost,
        "unpaired_yes_cost": unpaired_yes_cost,
        "unpaired_yes_pnl": unpaired_yes_payout - unpaired_yes_cost,
        "unpaired_no_cost": unpaired_no_cost,
        "unpaired_no_pnl": unpaired_no_payout - unpaired_no_cost,
        "unpaired_time_buckets": unpaired_buckets,
    }


def _unpaired_time_buckets(lots: Sequence[Mapping[str, Any]], *, outcome: str | None) -> dict[str, dict[str, float]]:
    buckets: dict[str, dict[str, float]] = defaultdict(lambda: {"contracts": 0.0, "cost": 0.0, "payout": 0.0, "pnl": 0.0})
    for lot in lots:
        bucket = _time_bucket_from_ts(_field(lot, "ts"))
        side = str(lot.get("side") or "")
        contracts = _as_float(lot.get("contracts"), 0.0)
        cost = _as_float(lot.get("cost"), 0.0)
        payout = contracts if (side == "yes" and outcome == "above") or (side == "no" and outcome == "below") else 0.0
        key = f"{side}:{bucket}"
        row = buckets[key]
        row["contracts"] += contracts
        row["cost"] += cost
        row["payout"] += payout
        row["pnl"] += payout - cost
    return {key: {metric: round(value, 6) for metric, value in row.items()} for key, row in sorted(buckets.items())}


def _time_bucket_from_ts(value: Any) -> str:
    try:
        minute = datetime.fromisoformat(str(value).replace("Z", "+00:00")).minute
    except (TypeError, ValueError):
        return "unknown"
    remaining = 15 - (minute % 15)
    if remaining <= 2:
        return "00-02m"
    if remaining <= 4:
        return "02-04m"
    if remaining <= 8:
        return "04-08m"
    if remaining <= 12:
        return "08-12m"
    return "12-15m"


def _aggregate_pnl_split(markets: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, dict[str, float]] = defaultdict(lambda: {"contracts": 0.0, "cost": 0.0, "payout": 0.0, "pnl": 0.0})
    for market in markets:
        for key, row in dict(market.get("unpaired_time_buckets") or {}).items():
            dest = buckets[key]
            for metric in ("contracts", "cost", "payout", "pnl"):
                dest[metric] += _as_float(row.get(metric), 0.0)
    return {
        "completed_pair_contracts": sum(_as_float(m.get("completed_pair_contracts"), 0.0) for m in markets),
        "completed_pair_cost": sum(_as_float(m.get("completed_pair_cost"), 0.0) for m in markets),
        "completed_pair_payout": sum(_as_float(m.get("completed_pair_payout"), 0.0) for m in markets),
        "completed_pair_pnl": sum(_as_float(m.get("completed_pair_pnl"), 0.0) for m in markets),
        "unpaired_leftover_cost": sum(_as_float(m.get("unpaired_leftover_cost"), 0.0) for m in markets),
        "unpaired_leftover_payout": sum(_as_float(m.get("unpaired_leftover_payout"), 0.0) for m in markets),
        "unpaired_leftover_pnl": sum(_as_float(m.get("unpaired_leftover_pnl"), 0.0) for m in markets),
        "unpaired_yes_pnl": sum(_as_float(m.get("unpaired_yes_pnl"), 0.0) for m in markets),
        "unpaired_no_pnl": sum(_as_float(m.get("unpaired_no_pnl"), 0.0) for m in markets),
        "unpaired_time_buckets": {key: {metric: round(value, 6) for metric, value in row.items()} for key, row in sorted(buckets.items())},
    }


def _market_date(market_ticker: str) -> str:
    match = re.search(r"(\d{2})([A-Z]{3})(\d{2})", market_ticker.upper())
    if not match:
        return "unknown"
    year, mon, day = match.groups()
    try:
        return datetime.strptime(f"20{year}{mon}{day}", "%Y%b%d").date().isoformat()
    except ValueError:
        return "unknown"


def _date_from_row(row: Mapping[str, Any] | Any) -> str | None:
    for key in ("market_close_time", "close_time", "ts", "created_at"):
        value = _field(row, key)
        if not value:
            continue
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            continue
    return None


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
                "date": _date_from_row(row) or _market_date(market_ticker),
            }
            continue

        price = _as_float(_first_present(row, "btc_price", "price", "settlement_price"), 0.0)
        strike = _as_float(_first_present(row, "target_price", "strike"), 0.0)
        if price > strike:
            outcomes[market_ticker] = {
                "outcome": "above",
                "source": SETTLEMENT_SOURCE_REPLAY_FINAL_SNAPSHOT,
                "date": _date_from_row(row) or _market_date(market_ticker),
            }
        elif price < strike:
            outcomes[market_ticker] = {
                "outcome": "below",
                "source": SETTLEMENT_SOURCE_REPLAY_FINAL_SNAPSHOT,
                "date": _date_from_row(row) or _market_date(market_ticker),
            }
        else:
            outcomes[market_ticker] = {
                "outcome": "at",
                "source": SETTLEMENT_SOURCE_REPLAY_FINAL_SNAPSHOT,
                "date": _date_from_row(row) or _market_date(market_ticker),
            }
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
