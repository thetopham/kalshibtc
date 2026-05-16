from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SETTLEMENT_SOURCE_OFFICIAL = "kalshi_official"
SETTLEMENT_SOURCE_REPLAY_FINAL_SNAPSHOT = "replay_final_snapshot"
SETTLEMENT_SOURCE_UNSETTLED = "unsettled_no_snapshot"


def estimate_replay_fill_pnls(
    fills: Sequence[Mapping[str, Any] | Any],
    settlement_rows: Sequence[Mapping[str, Any] | Any],
    *,
    official_settlements: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Estimate replay fill PnL, preferring official market settlement rows.

    Official rows are read from the feed DB's one-row-per-market
    ``market_settlements`` table. The final snapshot fallback is kept only for
    research windows where official settlement is not yet available.
    """
    snapshot_outcomes = _settlement_outcomes(settlement_rows)
    official = official_settlements or {}
    settled: list[dict[str, Any]] = []
    for fill in fills:
        market_ticker = str(_field(fill, "market_ticker") or "")
        official_row = official.get(market_ticker)
        if official_row is not None:
            outcome = str(official_row.get("settlement_result") or "")
            source = str(official_row.get("settlement_source") or SETTLEMENT_SOURCE_OFFICIAL)
            extra = {
                "official_result": official_row.get("official_result"),
                "official_expiration_value": official_row.get("official_expiration_value"),
                "settlement_value_dollars": official_row.get("settlement_value_dollars"),
                "settlement_raw_json": official_row.get("settlement_raw_json"),
            }
        else:
            outcome = snapshot_outcomes.get(market_ticker)
            source = SETTLEMENT_SOURCE_REPLAY_FINAL_SNAPSHOT if outcome is not None else SETTLEMENT_SOURCE_UNSETTLED
            extra = {}

        item = _fill_to_dict(fill)
        if not outcome:
            item.update(
                {
                    "pnl": 0.0,
                    "settlement_result": None,
                    "settlement_source": source,
                    "exit_price": None,
                }
            )
            item.update(extra)
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
                "settlement_source": source,
                "exit_price": exit_price,
            }
        )
        item.update(extra)
        settled.append(item)
    return settled


def ensure_market_settlements_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_settlements (
            market_ticker TEXT PRIMARY KEY,
            settlement_source TEXT NOT NULL,
            settlement_result TEXT NOT NULL,
            official_result TEXT,
            official_expiration_value REAL,
            settlement_value_dollars REAL,
            settlement_raw_json TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )


def load_official_market_settlements(feed_db: Path) -> dict[str, dict[str, Any]]:
    with sqlite3.connect(f"file:{feed_db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        if not _table_exists(conn, "market_settlements"):
            return {}
        rows = conn.execute(
            """
            SELECT *
            FROM market_settlements
            WHERE settlement_source = ?
            """,
            (SETTLEMENT_SOURCE_OFFICIAL,),
        ).fetchall()
    return {str(row["market_ticker"]): dict(row) for row in rows}


def backfill_market_settlements(feed_db: Path, *, official_client: Any) -> int:
    """Backfill official Kalshi settlement rows into the feed DB.

    This writes only to ``market_settlements``. It intentionally does not alter
    ``realtime_snapshots_1s`` rows.
    """
    with sqlite3.connect(feed_db) as conn:
        conn.row_factory = sqlite3.Row
        ensure_market_settlements_table(conn)
        market_tickers = [
            str(row["market_ticker"])
            for row in conn.execute(
                "SELECT DISTINCT market_ticker FROM realtime_snapshots_1s ORDER BY market_ticker"
            )
        ]
        written = 0
        for market_ticker in market_tickers:
            try:
                market = official_client.get_market(market_ticker)
            except Exception:
                continue
            settlement = official_settlement_from_market(market)
            if settlement is None:
                continue
            conn.execute(
                """
                INSERT INTO market_settlements (
                    market_ticker, settlement_source, settlement_result, official_result,
                    official_expiration_value, settlement_value_dollars, settlement_raw_json,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_ticker) DO UPDATE SET
                    settlement_source = excluded.settlement_source,
                    settlement_result = excluded.settlement_result,
                    official_result = excluded.official_result,
                    official_expiration_value = excluded.official_expiration_value,
                    settlement_value_dollars = excluded.settlement_value_dollars,
                    settlement_raw_json = excluded.settlement_raw_json,
                    updated_at = excluded.updated_at
                """,
                (
                    market_ticker,
                    SETTLEMENT_SOURCE_OFFICIAL,
                    settlement["settlement_result"],
                    settlement.get("official_result"),
                    settlement.get("official_expiration_value"),
                    settlement.get("settlement_value_dollars"),
                    settlement.get("settlement_raw_json"),
                    datetime.now(tz=UTC).isoformat(),
                ),
            )
            written += 1
        return written


def official_settlement_from_market(market: Mapping[str, Any]) -> dict[str, Any] | None:
    result = _first_text(
        market,
        "result",
        "settlement_result",
        "final_result",
        "winning_side",
        "outcome",
        "market_result",
    )
    outcome = _official_result_to_outcome(result)
    if outcome is None:
        return None
    official_result = _normalize_official_result(result, outcome)
    return {
        "settlement_result": outcome,
        "official_result": official_result,
        "official_expiration_value": _first_float_from_mapping(
            market,
            "expiration_value",
            "final_price",
            "underlying_price",
            "index_price",
        ),
        "settlement_value_dollars": _first_float_from_mapping(
            market,
            "settlement_value_dollars",
            "settlement_value",
            "payout_dollars",
        ),
        "settlement_raw_json": json.dumps(dict(market), sort_keys=True),
    }


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


def _official_result_to_outcome(result: str | None) -> str | None:
    if result is None:
        return None
    token = str(result).strip().lower().replace(" ", "_").replace("-", "_")
    if token in {"yes", "y", "true", "above", "yes_win", "yes_won", "yes_wins"}:
        return "above"
    if token in {"no", "n", "false", "below", "at_or_below", "no_win", "no_won", "no_wins"}:
        return "below"
    if token == "at":
        return "at"
    return None


def _normalize_official_result(result: str | None, outcome: str) -> str | None:
    if result is None:
        return None
    token = str(result).strip().lower()
    if token in {"yes", "no"}:
        return token
    if outcome == "above":
        return "yes"
    if outcome == "below":
        return "no"
    return token or None


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


def _first_text(mapping: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _first_float_from_mapping(mapping: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float_or_none(mapping.get(key))
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
    parsed = _float_or_none(value)
    return default if parsed is None else parsed


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None
