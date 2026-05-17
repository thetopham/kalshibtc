from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from kalshibtc.replay.log_expired_settlements import log_expired_settlements, main


def create_feed_db(path):
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE realtime_snapshots_1s (
            ts TEXT NOT NULL,
            market_ticker TEXT NOT NULL,
            market_open_time TEXT,
            market_close_time TEXT NOT NULL,
            btc_price REAL NOT NULL,
            strike REAL NOT NULL,
            target_price REAL,
            distance_from_strike REAL,
            seconds_to_close REAL,
            btc_velocity_30s REAL,
            slope_30s REAL,
            yes_bid REAL,
            yes_ask REAL,
            no_bid REAL,
            no_ask REAL,
            orderbook_sequence INTEGER,
            execution_blocked_by_json TEXT NOT NULL,
            raw_state_json TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (market_ticker, ts)
        )
        """
    )
    return con


def insert_snapshot(con, *, ticker, ts, close, price, strike):
    con.execute(
        """
        INSERT INTO realtime_snapshots_1s (
            ts, market_ticker, market_open_time, market_close_time, btc_price, strike,
            target_price, distance_from_strike, seconds_to_close, btc_velocity_30s,
            slope_30s, yes_bid, yes_ask, no_bid, no_ask, orderbook_sequence,
            execution_blocked_by_json, raw_state_json, raw_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ts.isoformat(),
            ticker,
            (close - timedelta(minutes=15)).isoformat(),
            close.isoformat(),
            price,
            strike,
            strike,
            price - strike,
            (close - ts).total_seconds(),
            0.0,
            0.0,
            0.49,
            0.51,
            0.49,
            0.51,
            1,
            "[]",
            "{}",
            json.dumps({"ticker": ticker}),
            ts.isoformat(),
        ),
    )


def test_settlement_logger_inserts_expired_markets(tmp_path):
    feed_db = tmp_path / "feed.sqlite3"
    con = create_feed_db(feed_db)
    now = datetime(2026, 5, 16, 15, 0, tzinfo=UTC)
    close = now - timedelta(minutes=5)
    insert_snapshot(con, ticker="KXBTC-EXPIRED", ts=close - timedelta(seconds=1), close=close, price=50_100, strike=50_000)
    con.commit()

    summary = log_expired_settlements(feed_db=feed_db, source="feed_last_price_proxy", grace_seconds=90, now=now)

    assert summary["inserted"] == 1
    row = con.execute("select market_ticker, settlement_price, winning_side, source, status from market_settlements").fetchone()
    assert row == ("KXBTC-EXPIRED", 50100.0, "yes", "feed_last_price_proxy", "settled_proxy")


def test_settlement_logger_skips_unexpired_markets(tmp_path):
    feed_db = tmp_path / "feed.sqlite3"
    con = create_feed_db(feed_db)
    now = datetime(2026, 5, 16, 15, 0, tzinfo=UTC)
    close = now + timedelta(minutes=1)
    insert_snapshot(con, ticker="KXBTC-OPEN", ts=now, close=close, price=49_900, strike=50_000)
    con.commit()

    summary = log_expired_settlements(feed_db=feed_db, source="feed_last_price_proxy", grace_seconds=90, now=now)

    assert summary["expired_markets"] == 0
    assert con.execute("select count(*) from market_settlements").fetchone()[0] == 0


def test_settlement_logger_is_idempotent(tmp_path):
    feed_db = tmp_path / "feed.sqlite3"
    con = create_feed_db(feed_db)
    now = datetime(2026, 5, 16, 15, 0, tzinfo=UTC)
    close = now - timedelta(minutes=5)
    insert_snapshot(con, ticker="KXBTC-EXPIRED", ts=close - timedelta(seconds=1), close=close, price=49_900, strike=50_000)
    con.commit()

    first = log_expired_settlements(feed_db=feed_db, source="feed_last_price_proxy", grace_seconds=90, now=now)
    second = log_expired_settlements(feed_db=feed_db, source="feed_last_price_proxy", grace_seconds=90, now=now)

    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["skipped_existing"] == 1
    assert con.execute("select count(*) from market_settlements").fetchone()[0] == 1


class FakeKalshiClient:
    def __init__(self, markets):
        self.markets = markets
        self.calls = []

    def get_market(self, ticker):
        self.calls.append(ticker)
        return self.markets[ticker]


def test_settlement_logger_pulls_kalshi_official_once_after_rollover(tmp_path):
    feed_db = tmp_path / "feed.sqlite3"
    con = create_feed_db(feed_db)
    now = datetime(2026, 5, 16, 15, 0, tzinfo=UTC)
    close = now - timedelta(minutes=5)
    insert_snapshot(con, ticker="KXBTC-OFFICIAL", ts=close - timedelta(seconds=1), close=close, price=49_900, strike=50_000)
    con.commit()
    client = FakeKalshiClient(
        {
            "KXBTC-OFFICIAL": {
                "ticker": "KXBTC-OFFICIAL",
                "result": "yes",
                "expiration_value": "50123.45",
                "close_time": close.isoformat(),
            }
        }
    )

    first = log_expired_settlements(
        feed_db=feed_db,
        source="kalshi_api",
        grace_seconds=90,
        now=now,
        kalshi_client=client,
    )
    second = log_expired_settlements(
        feed_db=feed_db,
        source="kalshi_api",
        grace_seconds=90,
        now=now + timedelta(minutes=1),
        kalshi_client=client,
    )

    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["skipped_existing"] == 1
    assert client.calls == ["KXBTC-OFFICIAL"]
    row = con.execute(
        "select market_ticker, settlement_price, winning_side, source, status, raw_json from market_settlements"
    ).fetchone()
    assert row[:5] == ("KXBTC-OFFICIAL", 50123.45, "yes", "kalshi_api", "settled_official")
    assert json.loads(row[5])["result"] == "yes"


def test_settlement_logger_keeps_existing_proxy_when_official_missing(tmp_path):
    feed_db = tmp_path / "feed.sqlite3"
    con = create_feed_db(feed_db)
    now = datetime(2026, 5, 16, 15, 0, tzinfo=UTC)
    close = now - timedelta(minutes=5)
    insert_snapshot(con, ticker="KXBTC-PROXY", ts=close - timedelta(seconds=1), close=close, price=49_900, strike=50_000)
    con.commit()
    log_expired_settlements(feed_db=feed_db, source="feed_last_price_proxy", grace_seconds=90, now=now)

    summary = log_expired_settlements(
        feed_db=feed_db,
        source="kalshi_api",
        grace_seconds=90,
        now=now,
        kalshi_client=FakeKalshiClient({"KXBTC-PROXY": {"ticker": "KXBTC-PROXY", "result": ""}}),
    )

    assert summary["missing"] == 1
    row = con.execute("select settlement_price, winning_side, source, status from market_settlements").fetchone()
    assert row == (49_900.0, "no", "feed_last_price_proxy", "settled_proxy")


def test_settlement_logger_cli_json(tmp_path, capsys):
    feed_db = tmp_path / "feed.sqlite3"
    con = create_feed_db(feed_db)
    now = datetime.now(UTC) - timedelta(minutes=5)
    close = now - timedelta(minutes=2)
    insert_snapshot(con, ticker="KXBTC-CLI", ts=close - timedelta(seconds=1), close=close, price=50_100, strike=50_000)
    con.commit()

    code = main(["--feed-db", str(feed_db), "--source", "feed_last_price_proxy", "--grace-seconds", "90", "--json"])

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["inserted"] == 1
