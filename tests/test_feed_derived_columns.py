from __future__ import annotations

import sqlite3
from pathlib import Path

from kalshibtc.feed_derived_columns import backfill_feed_derived_columns, ensure_feed_derived_columns


def test_backfill_polymarket_feed_derived_columns_from_raw_json(tmp_path: Path) -> None:
    db_path = tmp_path / "poly.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE realtime_snapshots_1s (
                ts TEXT NOT NULL,
                market_slug TEXT NOT NULL,
                market_ticker TEXT,
                yes_bid REAL,
                yes_ask REAL,
                no_bid REAL,
                no_ask REAL,
                yes_orderbook_json TEXT,
                no_orderbook_json TEXT,
                raw_market_json TEXT,
                raw_json TEXT,
                PRIMARY KEY (market_slug, ts)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO realtime_snapshots_1s (
                ts, market_slug, market_ticker, yes_bid, yes_ask, no_bid, no_ask,
                yes_orderbook_json, no_orderbook_json, raw_market_json, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-05-18T04:45:01+00:00",
                "btc-updown-15m-1779089400",
                "btc-updown-15m-1779089400",
                0.49,
                0.51,
                0.48,
                0.52,
                '{"bids":[{"price":"0.49","size":"20"},{"price":"0.48","size":"7"}],"asks":[{"price":"0.51","size":"15"},{"price":"0.52","size":"5"}]}',
                '{"bids":[{"price":"0.48","size":"30"}],"asks":[{"price":"0.52","size":"11"},{"price":"0.53","size":"4"}]}',
                '{"volume":"123.45","volume24hr":"67.89","liquidity":"1000.5"}',
                "{}",
            ),
        )

    result = backfill_feed_derived_columns(db_path, venue="polymarket")

    assert result.rows_seen == 1
    assert result.rows_updated == 1
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM realtime_snapshots_1s").fetchone()
    assert row["yes_spread"] == 0.02
    assert row["no_spread"] == 0.04
    assert row["yes_top_bid_size"] == 20.0
    assert row["yes_top_ask_size"] == 15.0
    assert row["no_top_bid_size"] == 30.0
    assert row["no_top_ask_size"] == 11.0
    assert row["yes_bid_depth_5_ticks"] == 27.0
    assert row["yes_ask_depth_5_ticks"] == 20.0
    assert row["no_bid_depth_5_ticks"] == 30.0
    assert row["no_ask_depth_5_ticks"] == 15.0
    assert row["orderbook_imbalance"] == 0.23913
    assert row["liquidity"] == 1000.5
    assert row["volume"] == 123.45
    assert row["volume_24h"] == 67.89


def test_backfill_kalshi_feed_derived_columns_from_raw_orderbook(tmp_path: Path) -> None:
    db_path = tmp_path / "kalshi.sqlite3"
    raw = {
        "orderbook_raw": {
            "orderbook_fp": {
                "yes_dollars": [["0.49", "20"], ["0.48", "7"]],
                "no_dollars": [["0.48", "30"], ["0.47", "5"]],
            }
        },
        "market_raw": {"volume": 321, "liquidity": 654},
    }
    import json

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE realtime_snapshots_1s (
                ts TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                yes_bid REAL,
                yes_ask REAL,
                no_bid REAL,
                no_ask REAL,
                raw_json TEXT,
                PRIMARY KEY (market_ticker, ts)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO realtime_snapshots_1s (
                ts, market_ticker, yes_bid, yes_ask, no_bid, no_ask, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("2026-05-15T12:00:03+00:00", "KXBTC15M-TEST", 0.49, 0.52, 0.48, 0.51, json.dumps(raw)),
        )

    ensure_feed_derived_columns(db_path)
    result = backfill_feed_derived_columns(db_path, venue="kalshi")

    assert result.rows_seen == 1
    assert result.rows_updated == 1
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM realtime_snapshots_1s").fetchone()
    assert row["yes_spread"] == 0.03
    assert row["no_spread"] == 0.03
    assert row["yes_top_bid_size"] == 20.0
    assert row["yes_bid_depth_5_ticks"] == 27.0
    assert row["no_top_bid_size"] == 30.0
    assert row["no_bid_depth_5_ticks"] == 35.0
    assert row["orderbook_imbalance"] == 0.0
    assert row["liquidity"] == 654.0
    assert row["volume"] == 321.0
