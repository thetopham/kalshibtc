from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from ..runtime_paths import DEFAULT_FEED_DB, resolve_feed_db

SettlementLoggerSource = Literal["feed_last_price_proxy", "kalshi_api"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kbtc-log-expired-settlements",
        description="Log one local settlement row per expired BTC 15m market in the feed DB.",
    )
    parser.add_argument("--feed-db", default=None, help=f"Feed SQLite DB. Default: {DEFAULT_FEED_DB}")
    parser.add_argument("--source", choices=["feed_last_price_proxy", "kalshi_api"], default="feed_last_price_proxy")
    parser.add_argument("--grace-seconds", type=float, default=90.0)
    parser.add_argument("--json", action="store_true", help="Print JSON summary.")
    args = parser.parse_args(argv)

    summary = log_expired_settlements(
        feed_db=resolve_feed_db(args.feed_db),
        source=args.source,
        grace_seconds=args.grace_seconds,
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
    source: SettlementLoggerSource = "feed_last_price_proxy",
    grace_seconds: float = 90.0,
    now: datetime | None = None,
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
            if _settlement_exists(conn, str(market["market_ticker"])):
                skipped_existing += 1
                continue
            row = _settlement_row(conn, market=market, source=source, fetched_at=current_time)
            if row["status"] == "missing":
                missing += 1
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


def _settlement_exists(conn: sqlite3.Connection, market_ticker: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM market_settlements WHERE market_ticker = ?",
        (market_ticker,),
    ).fetchone() is not None


def _settlement_row(
    conn: sqlite3.Connection,
    *,
    market: sqlite3.Row,
    source: SettlementLoggerSource,
    fetched_at: datetime,
) -> dict[str, Any]:
    market_ticker = str(market["market_ticker"])
    strike = float(market["strike"])
    close_time = str(market["market_close_time"])
    raw: dict[str, Any] = {"source": source, "mode": "local_settlement_logger"}
    if source == "kalshi_api":
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


if __name__ == "__main__":
    raise SystemExit(main())
