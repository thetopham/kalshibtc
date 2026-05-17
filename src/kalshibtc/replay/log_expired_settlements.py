from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from ..market.kalshi_public import KalshiPublicClient
from ..runtime_paths import DEFAULT_FEED_DB, resolve_feed_db

SettlementLoggerSource = Literal["feed_last_price_proxy", "kalshi_api"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kbtc-log-expired-settlements",
        description="Log one local settlement row per expired BTC 15m market in the feed DB.",
    )
    parser.add_argument("--feed-db", default=None, help=f"Feed SQLite DB. Default: {DEFAULT_FEED_DB}")
    parser.add_argument("--source", choices=["feed_last_price_proxy", "kalshi_api"], default="kalshi_api")
    parser.add_argument("--kalshi-base-url", default="https://external-api.kalshi.com/trade-api/v2")
    parser.add_argument("--timeout-seconds", type=int, default=20)
    parser.add_argument("--grace-seconds", type=float, default=90.0)
    parser.add_argument("--json", action="store_true", help="Print JSON summary.")
    args = parser.parse_args(argv)

    summary = log_expired_settlements(
        feed_db=resolve_feed_db(args.feed_db),
        source=args.source,
        grace_seconds=args.grace_seconds,
        kalshi_client=KalshiPublicClient(args.kalshi_base_url, timeout_seconds=args.timeout_seconds)
        if args.source == "kalshi_api"
        else None,
    )
    if args.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print(
            "log_expired_settlements "
            f"expired_markets={summary['expired_markets']} "
            f"inserted={summary['inserted']} "
            f"skipped_existing={summary['skipped_existing']} "
            f"source={summary['source']}"
        )
    return 0


def log_expired_settlements(
    *,
    feed_db: str | Path,
    source: SettlementLoggerSource = "kalshi_api",
    grace_seconds: float = 90.0,
    now: datetime | None = None,
    kalshi_client: Any | None = None,
) -> dict[str, Any]:
    db_path = Path(feed_db)
    current_time = (now or datetime.now(tz=UTC)).astimezone(UTC)
    cutoff = current_time - timedelta(seconds=float(grace_seconds))
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_market_settlements_schema(conn)
        expired_markets = _expired_markets(conn, cutoff=cutoff)
        inserted = 0
        skipped_existing = 0
        missing = 0
        for market in expired_markets:
            if _settlement_exists(conn, str(market["market_ticker"]), source=source):
                skipped_existing += 1
                continue
            row = _settlement_row(conn, market=market, source=source, fetched_at=current_time, kalshi_client=kalshi_client)
            if row["status"] == "missing":
                missing += 1
                continue
            if row["status"] == "upgrade_existing_proxy":
                conn.execute(
                    """
                    UPDATE market_settlements
                    SET settlement_price = ?, winning_side = ?, source = ?, status = ?,
                        settled_at = ?, fetched_at = ?, raw_json = ?
                    WHERE market_ticker = ?
                    """,
                    (
                        row["settlement_price"],
                        row["winning_side"],
                        row["source"],
                        "settled_official",
                        row["settled_at"],
                        row["fetched_at"],
                        row["raw_json"],
                        row["market_ticker"],
                    ),
                )
                inserted += 1
                continue
            conn.execute(
                """
                INSERT INTO market_settlements (
                    market_ticker, market_open_time, market_close_time, strike,
                    settlement_price, winning_side, source, status,
                    settled_at, fetched_at, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["market_ticker"],
                    row["market_open_time"],
                    row["market_close_time"],
                    row["strike"],
                    row["settlement_price"],
                    row["winning_side"],
                    row["source"],
                    row["status"],
                    row["settled_at"],
                    row["fetched_at"],
                    row["raw_json"],
                ),
            )
            inserted += 1
        conn.commit()
    return {
        "feed_db": str(db_path),
        "source": source,
        "grace_seconds": float(grace_seconds),
        "cutoff": cutoff.isoformat(),
        "expired_markets": len(expired_markets),
        "inserted": inserted,
        "skipped_existing": skipped_existing,
        "missing": missing,
    }


def ensure_market_settlements_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_settlements (
            market_ticker TEXT PRIMARY KEY,
            market_open_time TEXT,
            market_close_time TEXT,
            strike REAL,
            settlement_price REAL,
            winning_side TEXT,
            source TEXT,
            status TEXT,
            settled_at TEXT,
            fetched_at TEXT,
            raw_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_market_settlements_close_time
            ON market_settlements(market_close_time)
        """
    )


def _expired_markets(conn: sqlite3.Connection, *, cutoff: datetime) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT
                market_ticker,
                min(market_open_time) AS market_open_time,
                max(market_close_time) AS market_close_time,
                max(strike) AS strike
            FROM realtime_snapshots_1s
            WHERE market_close_time < ?
            GROUP BY market_ticker
            ORDER BY market_close_time ASC, market_ticker ASC
            """,
            (cutoff.isoformat(),),
        )
    )


def _settlement_exists(conn: sqlite3.Connection, market_ticker: str, *, source: SettlementLoggerSource) -> bool:
    row = conn.execute(
        "SELECT source, status FROM market_settlements WHERE market_ticker = ?",
        (market_ticker,),
    ).fetchone()
    if row is None:
        return False
    if source == "kalshi_api":
        return str(row["source"]) == "kalshi_api" and str(row["status"]) == "settled_official"
    return True


def _settlement_row(
    conn: sqlite3.Connection,
    *,
    market: sqlite3.Row,
    source: SettlementLoggerSource,
    fetched_at: datetime,
    kalshi_client: Any | None = None,
) -> dict[str, Any]:
    market_ticker = str(market["market_ticker"])
    strike = float(market["strike"])
    close_time = str(market["market_close_time"])
    raw: dict[str, Any] = {"source": source, "mode": "local_settlement_logger"}
    if source == "kalshi_api":
        if kalshi_client is None:
            return {
                "market_ticker": market_ticker,
                "market_open_time": market["market_open_time"],
                "market_close_time": close_time,
                "strike": strike,
                "settlement_price": None,
                "winning_side": None,
                "source": source,
                "status": "missing",
                "settled_at": close_time,
                "fetched_at": fetched_at.isoformat(),
                "raw_json": json.dumps({**raw, "reason": "kalshi_api_not_configured"}, sort_keys=True),
            }
        return _kalshi_api_settlement_row(
            conn,
            market=market,
            market_ticker=market_ticker,
            strike=strike,
            close_time=close_time,
            fetched_at=fetched_at,
            kalshi_client=kalshi_client,
        )

    price_row = conn.execute(
        """
        SELECT ts, btc_price, raw_json
        FROM realtime_snapshots_1s
        WHERE market_ticker = ? AND ts <= ?
        ORDER BY ts DESC
        LIMIT 1
        """,
        (market_ticker, close_time),
    ).fetchone()
    if price_row is None:
        price_row = conn.execute(
            """
            SELECT ts, btc_price, raw_json
            FROM realtime_snapshots_1s
            WHERE market_ticker = ?
            ORDER BY ts DESC
            LIMIT 1
            """,
            (market_ticker,),
        ).fetchone()
    if price_row is None:
        return {
            "market_ticker": market_ticker,
            "market_open_time": market["market_open_time"],
            "market_close_time": close_time,
            "strike": strike,
            "settlement_price": None,
            "winning_side": None,
            "source": source,
            "status": "missing",
            "settled_at": close_time,
            "fetched_at": fetched_at.isoformat(),
            "raw_json": json.dumps({**raw, "reason": "no_feed_price"}, sort_keys=True),
        }
    settlement_price = float(price_row["btc_price"])
    return {
        "market_ticker": market_ticker,
        "market_open_time": market["market_open_time"],
        "market_close_time": close_time,
        "strike": strike,
        "settlement_price": settlement_price,
        "winning_side": "yes" if settlement_price > strike else "no",
        "source": source,
        "status": "settled_proxy",
        "settled_at": close_time,
        "fetched_at": fetched_at.isoformat(),
        "raw_json": json.dumps(
            {
                **raw,
                "price_ts": price_row["ts"],
                "price_source": "last_btc_price_at_or_before_close",
            },
            sort_keys=True,
        ),
    }


def _kalshi_api_settlement_row(
    conn: sqlite3.Connection,
    *,
    market: sqlite3.Row,
    market_ticker: str,
    strike: float,
    close_time: str,
    fetched_at: datetime,
    kalshi_client: Any,
) -> dict[str, Any]:
    try:
        raw_market = kalshi_client.get_market(market_ticker)
    except Exception as exc:
        return _missing_row(
            market=market,
            market_ticker=market_ticker,
            strike=strike,
            close_time=close_time,
            fetched_at=fetched_at,
            source="kalshi_api",
            reason=f"kalshi_api_error:{type(exc).__name__}",
        )
    if not isinstance(raw_market, dict):
        raw_market = {}
    nested = raw_market.get("market")
    if isinstance(nested, dict):
        raw_market = nested
    result = _first_text(raw_market, "result", "settlement_result", "final_result", "winning_side", "outcome", "market_result")
    winning_side = _normalize_winning_side(result)
    settlement_price = _first_float(raw_market, "expiration_value", "final_price", "underlying_price", "index_price")
    if winning_side is None and settlement_price is not None:
        winning_side = "yes" if settlement_price > strike else "no"
    if settlement_price is None:
        settlement_price = _proxy_price_for_missing_official_value(conn, market_ticker=market_ticker, close_time=close_time)
    if winning_side is None:
        return _missing_row(
            market=market,
            market_ticker=market_ticker,
            strike=strike,
            close_time=close_time,
            fetched_at=fetched_at,
            source="kalshi_api",
            reason="kalshi_result_missing",
            raw_market=raw_market,
        )
    status = "upgrade_existing_proxy" if _existing_proxy_settlement(conn, market_ticker) else "settled_official"
    return {
        "market_ticker": market_ticker,
        "market_open_time": market["market_open_time"],
        "market_close_time": close_time,
        "strike": strike,
        "settlement_price": settlement_price,
        "winning_side": winning_side,
        "source": "kalshi_api",
        "status": status,
        "settled_at": _first_text(raw_market, "settled_at", "settlement_ts", "expiration_time", "close_time") or close_time,
        "fetched_at": fetched_at.isoformat(),
        "raw_json": json.dumps({"source": "kalshi_api", "mode": "local_settlement_logger", **raw_market}, sort_keys=True),
    }


def _missing_row(
    *,
    market: sqlite3.Row,
    market_ticker: str,
    strike: float,
    close_time: str,
    fetched_at: datetime,
    source: SettlementLoggerSource,
    reason: str,
    raw_market: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "market_ticker": market_ticker,
        "market_open_time": market["market_open_time"],
        "market_close_time": close_time,
        "strike": strike,
        "settlement_price": None,
        "winning_side": None,
        "source": source,
        "status": "missing",
        "settled_at": close_time,
        "fetched_at": fetched_at.isoformat(),
        "raw_json": json.dumps(
            {"source": source, "mode": "local_settlement_logger", "reason": reason, "market": raw_market or {}},
            sort_keys=True,
        ),
    }


def _existing_proxy_settlement(conn: sqlite3.Connection, market_ticker: str) -> bool:
    row = conn.execute("SELECT source, status FROM market_settlements WHERE market_ticker = ?", (market_ticker,)).fetchone()
    return row is not None and str(row["source"]) != "kalshi_api"


def _proxy_price_for_missing_official_value(conn: sqlite3.Connection, *, market_ticker: str, close_time: str) -> float | None:
    row = conn.execute(
        """
        SELECT btc_price FROM realtime_snapshots_1s
        WHERE market_ticker = ? AND ts <= ?
        ORDER BY ts DESC LIMIT 1
        """,
        (market_ticker, close_time),
    ).fetchone()
    return float(row["btc_price"]) if row is not None and row["btc_price"] is not None else None


def _normalize_winning_side(result: str | None) -> str | None:
    if result is None:
        return None
    token = str(result).strip().lower().replace(" ", "_").replace("-", "_")
    if token in {"yes", "y", "true", "above", "yes_win", "yes_won", "yes_wins"}:
        return "yes"
    if token in {"no", "n", "false", "below", "at_or_below", "no_win", "no_won", "no_wins"}:
        return "no"
    return None


def _first_text(mapping: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _first_float(mapping: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = mapping.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


if __name__ == "__main__":
    raise SystemExit(main())
