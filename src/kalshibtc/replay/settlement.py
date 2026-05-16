from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

SETTLEMENT_SOURCE_REPLAY_FINAL_SNAPSHOT = "replay_final_snapshot"


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
        outcome = outcomes.get(market_ticker)
        item = _fill_to_dict(fill)
        if outcome is None:
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
                "settlement_source": SETTLEMENT_SOURCE_REPLAY_FINAL_SNAPSHOT,
                "exit_price": exit_price,
            }
        )
        settled.append(item)
    return settled


def _settlement_outcomes(rows: Sequence[Mapping[str, Any] | Any]) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    for row in rows:
        market_ticker = str(_field(row, "market_ticker") or "")
        if not market_ticker:
            continue
        price = _as_float(_first_present(row, "btc_price", "price"), 0.0)
        strike = _as_float(_first_present(row, "target_price", "strike"), 0.0)
        if price > strike:
            outcomes[market_ticker] = "above"
        elif price < strike:
            outcomes[market_ticker] = "below"
        else:
            outcomes[market_ticker] = "at"
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
