from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STREAM_TABLE = "realtime_snapshots_1s"

DERIVED_COLUMN_DEFS = {
    "yes_spread": "REAL",
    "no_spread": "REAL",
    "yes_top_bid_size": "REAL",
    "yes_top_ask_size": "REAL",
    "no_top_bid_size": "REAL",
    "no_top_ask_size": "REAL",
    "yes_bid_depth_5_ticks": "REAL",
    "yes_ask_depth_5_ticks": "REAL",
    "no_bid_depth_5_ticks": "REAL",
    "no_ask_depth_5_ticks": "REAL",
    "orderbook_imbalance": "REAL",
    "liquidity": "REAL",
    "volume": "REAL",
    "volume_24h": "REAL",
}


@dataclass(frozen=True)
class BackfillResult:
    rows_seen: int
    rows_updated: int
    db_path: Path
    venue: str


def ensure_feed_derived_columns(db_path: str | Path) -> None:
    path = Path(db_path)
    with sqlite3.connect(path) as conn:
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({STREAM_TABLE})")}
        for column, definition in DERIVED_COLUMN_DEFS.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE {STREAM_TABLE} ADD COLUMN {column} {definition}")


def backfill_feed_derived_columns(
    db_path: str | Path,
    *,
    venue: str,
    batch_size: int = 500,
    only_missing: bool = False,
) -> BackfillResult:
    path = Path(db_path)
    ensure_feed_derived_columns(path)
    rows_seen = 0
    rows_updated = 0
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        key_column = "market_slug" if _has_column(conn, "market_slug") else "market_ticker"
        where = " WHERE yes_spread IS NULL OR orderbook_imbalance IS NULL" if only_missing else ""
        rows = conn.execute(f"SELECT rowid, * FROM {STREAM_TABLE}{where} ORDER BY ts ASC").fetchall()
        for row in rows:
            rows_seen += 1
            derived = derive_snapshot_columns(row, venue=venue)
            if not derived:
                continue
            assignments = ", ".join(f"{column} = ?" for column in derived)
            values = [derived[column] for column in derived]
            values.append(row["rowid"])
            conn.execute(f"UPDATE {STREAM_TABLE} SET {assignments} WHERE rowid = ?", values)
            rows_updated += 1
            if rows_updated % max(1, batch_size) == 0:
                conn.commit()
        conn.commit()
    return BackfillResult(rows_seen=rows_seen, rows_updated=rows_updated, db_path=path, venue=venue)


def derive_snapshot_columns(row: Mapping[str, Any] | sqlite3.Row, *, venue: str) -> dict[str, float | None]:
    yes_bid = _field_float(row, "yes_bid")
    yes_ask = _field_float(row, "yes_ask")
    no_bid = _field_float(row, "no_bid")
    no_ask = _field_float(row, "no_ask")
    yes_book, no_book, market = _books_and_market(row, venue=venue)
    yes_bid_levels = _bid_levels(yes_book, venue=venue, side="yes")
    yes_ask_levels = _ask_levels(yes_book, venue=venue, side="yes")
    no_bid_levels = _bid_levels(no_book, venue=venue, side="no")
    no_ask_levels = _ask_levels(no_book, venue=venue, side="no")

    yes_bid_depth = _depth(yes_bid_levels, limit=5)
    yes_ask_depth = _depth(yes_ask_levels, limit=5)
    no_bid_depth = _depth(no_bid_levels, limit=5)
    no_ask_depth = _depth(no_ask_levels, limit=5)
    total_bid_depth = yes_bid_depth + no_bid_depth
    total_ask_depth = yes_ask_depth + no_ask_depth
    denom = total_bid_depth + total_ask_depth
    imbalance = None if denom <= 0 else round((total_bid_depth - total_ask_depth) / denom, 6)

    return {
        "yes_spread": _spread(yes_bid, yes_ask),
        "no_spread": _spread(no_bid, no_ask),
        "yes_top_bid_size": _top_size(yes_bid_levels),
        "yes_top_ask_size": _top_size(yes_ask_levels),
        "no_top_bid_size": _top_size(no_bid_levels),
        "no_top_ask_size": _top_size(no_ask_levels),
        "yes_bid_depth_5_ticks": round(yes_bid_depth, 6),
        "yes_ask_depth_5_ticks": round(yes_ask_depth, 6),
        "no_bid_depth_5_ticks": round(no_bid_depth, 6),
        "no_ask_depth_5_ticks": round(no_ask_depth, 6),
        "orderbook_imbalance": imbalance,
        "liquidity": _first_float_from_mapping(market, "liquidity", "liquidityClob", "liquidityNum"),
        "volume": _first_float_from_mapping(market, "volume", "volumeClob", "volumeNum"),
        "volume_24h": _first_float_from_mapping(market, "volume24hr", "volume24hrClob"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kbtc-feed-derived-columns",
        description="Add/backfill normalized volume, liquidity, spread, and orderbook-depth columns in feed DBs.",
    )
    parser.add_argument("--db", "--feed-db", required=True)
    parser.add_argument("--venue", choices=["kalshi", "polymarket"], required=True)
    parser.add_argument("--only-missing", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = backfill_feed_derived_columns(args.db, venue=args.venue, only_missing=args.only_missing)
    payload = {
        "db_path": str(result.db_path),
        "venue": result.venue,
        "rows_seen": result.rows_seen,
        "rows_updated": result.rows_updated,
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(
            "kbtc_feed_derived_columns "
            f"venue={result.venue} db={result.db_path} rows_seen={result.rows_seen} rows_updated={result.rows_updated}"
        )
    return 0


def _books_and_market(row: Mapping[str, Any] | sqlite3.Row, *, venue: str) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    if venue == "polymarket":
        yes_book = _json_mapping(_field(row, "yes_orderbook_json"))
        no_book = _json_mapping(_field(row, "no_orderbook_json"))
        raw_book = _json_mapping(_field(row, "raw_book_json"))
        if not yes_book:
            yes_book = _mapping(raw_book.get("yes"))
        if not no_book:
            no_book = _mapping(raw_book.get("no"))
        market = _json_mapping(_field(row, "raw_market_json"))
        if not market:
            raw = _json_mapping(_field(row, "raw_json"))
            market = _mapping(raw.get("raw_market") or raw.get("snapshot", {}).get("raw_market"))
        return yes_book, no_book, market

    raw = _json_mapping(_field(row, "raw_json"))
    market = _mapping(raw.get("market_raw"))
    orderbook_raw = _mapping(raw.get("orderbook_raw"))
    orderbook = _mapping(orderbook_raw.get("orderbook_fp") or orderbook_raw.get("orderbook") or orderbook_raw)
    return orderbook, orderbook, market


def _bid_levels(book: Mapping[str, Any], *, venue: str, side: str) -> list[tuple[float, float]]:
    if venue == "polymarket":
        return _dict_levels(book.get("bids"), reverse=True)
    key = f"{side}_dollars"
    return _array_levels(book.get(key), reverse=True)


def _ask_levels(book: Mapping[str, Any], *, venue: str, side: str) -> list[tuple[float, float]]:
    if venue == "polymarket":
        return _dict_levels(book.get("asks"), reverse=False)
    opposite = "no" if side == "yes" else "yes"
    # Kalshi stores bid ladders. The ask for YES is 1 - best NO bid, and ask depth
    # is the opposite side's bid depth at complementary prices.
    return [(round(1.0 - price, 6), size) for price, size in _array_levels(book.get(f"{opposite}_dollars"), reverse=True)]


def _dict_levels(value: Any, *, reverse: bool) -> list[tuple[float, float]]:
    levels: list[tuple[float, float]] = []
    if not isinstance(value, list):
        return levels
    for item in value:
        if not isinstance(item, Mapping):
            continue
        price = _float_or_none(item.get("price"))
        size = _float_or_none(item.get("size"))
        if price is not None and size is not None:
            levels.append((price, size))
    return sorted(levels, key=lambda pair: pair[0], reverse=reverse)


def _array_levels(value: Any, *, reverse: bool) -> list[tuple[float, float]]:
    levels: list[tuple[float, float]] = []
    if not isinstance(value, list):
        return levels
    for item in value:
        if not isinstance(item, list | tuple) or len(item) < 2:
            continue
        price = _float_or_none(item[0])
        size = _float_or_none(item[1])
        if price is not None and size is not None:
            levels.append((price, size))
    return sorted(levels, key=lambda pair: pair[0], reverse=reverse)


def _depth(levels: Sequence[tuple[float, float]], *, limit: int) -> float:
    return sum(size for _, size in levels[:limit])


def _top_size(levels: Sequence[tuple[float, float]]) -> float | None:
    return round(levels[0][1], 6) if levels else None


def _spread(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    return round(max(0.0, ask - bid), 6)


def _has_column(conn: sqlite3.Connection, column: str) -> bool:
    return any(str(row[1]) == column for row in conn.execute(f"PRAGMA table_info({STREAM_TABLE})"))


def _field(row: Mapping[str, Any] | sqlite3.Row, key: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _field_float(row: Mapping[str, Any] | sqlite3.Row, key: str) -> float | None:
    return _float_or_none(_field(row, key))


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_float_from_mapping(mapping: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float_or_none(mapping.get(key))
        if value is not None:
            return value
    return None


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
