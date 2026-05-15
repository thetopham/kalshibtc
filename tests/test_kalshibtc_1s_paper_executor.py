from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kalshibtc.config import RiskLimits
from kalshibtc.paper_signal_executor import OneSecondPaperTrader, count_paper_trades
from kalshibtc.strategy.simple_directional import SimpleDirectionalStrategy


def _iso(seconds: int) -> str:
    return datetime(2026, 5, 15, 12, 0, tzinfo=UTC).replace(second=seconds).isoformat()


def _snapshot_db(path: Path, *, rows: list[dict[str, object]]) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
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
                raw_json TEXT
            )
            """
        )
        for row in rows:
            values = {
                "market_open_time": datetime(2026, 5, 15, 11, 45, tzinfo=UTC).isoformat(),
                "target_price": row.get("strike", 100_000.0),
                "distance_from_strike": float(row.get("btc_price", 0.0)) - float(row.get("strike", 100_000.0)),
                "seconds_to_close": 60.0,
                "btc_velocity_30s": row.get("slope_30s", row.get("btc_velocity_30s", 0.0)),
                "slope_30s": row.get("btc_velocity_30s", row.get("slope_30s", 0.0)),
                "yes_bid": 0.52,
                "yes_ask": 0.54,
                "no_bid": 0.44,
                "no_ask": 0.46,
                "raw_json": "{}",
                **row,
            }
            conn.execute(
                """
                INSERT INTO realtime_snapshots_1s (
                    ts, market_ticker, market_open_time, market_close_time,
                    btc_price, strike, target_price, distance_from_strike,
                    seconds_to_close, btc_velocity_30s, slope_30s,
                    yes_bid, yes_ask, no_bid, no_ask, raw_json
                ) VALUES (
                    :ts, :market_ticker, :market_open_time, :market_close_time,
                    :btc_price, :strike, :target_price, :distance_from_strike,
                    :seconds_to_close, :btc_velocity_30s, :slope_30s,
                    :yes_bid, :yes_ask, :no_bid, :no_ask, :raw_json
                )
                """,
                values,
            )


def _ledger_rows(path: Path, table: str) -> list[sqlite3.Row]:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return list(conn.execute(f"SELECT * FROM {table} ORDER BY created_at, id"))


def test_1s_paper_trader_writes_signal_fake_fill_and_expiry_pnl(tmp_path: Path) -> None:
    snapshot_db = tmp_path / "snapshots.sqlite3"
    results_db = tmp_path / "paper-results-1s.sqlite3"
    close_time = datetime(2026, 5, 15, 12, 0, 50, tzinfo=UTC)
    _snapshot_db(
        snapshot_db,
        rows=[
            {
                "ts": _iso(20),
                "market_ticker": "KXBTC15M-TEST",
                "market_close_time": close_time.isoformat(),
                "btc_price": 100_020.0,
                "strike": 100_000.0,
                "btc_velocity_30s": 2.5,
                "seconds_to_close": 30.0,
            },
            {
                "ts": _iso(51),
                "market_ticker": "KXBTC15M-TEST",
                "market_close_time": close_time.isoformat(),
                "btc_price": 100_080.0,
                "strike": 100_000.0,
                "btc_velocity_30s": 1.0,
                "seconds_to_close": -1.0,
                "yes_bid": 1.0,
                "yes_ask": 1.0,
                "no_bid": 0.0,
                "no_ask": 0.0,
            },
        ],
    )

    trader = OneSecondPaperTrader(
        snapshot_db=snapshot_db,
        ledger_db=results_db,
        strategies=[SimpleDirectionalStrategy()],
        risk_limits=RiskLimits(base_size_dollars=25.0, max_position_dollars=25.0, max_spread=0.05),
    )
    summary = trader.run_once(limit=10)

    assert summary.snapshots_processed == 2
    assert summary.signals_recorded == 2
    assert summary.trades_opened == 1
    assert summary.trades_closed == 1
    assert count_paper_trades(results_db) == 1

    with sqlite3.connect(snapshot_db) as conn:
        stream_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        conn.execute(
            """
            INSERT INTO realtime_snapshots_1s (
                ts, market_ticker, market_open_time, market_close_time,
                btc_price, strike, target_price, distance_from_strike,
                seconds_to_close, btc_velocity_30s, slope_30s,
                yes_bid, yes_ask, no_bid, no_ask, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _iso(52),
                "KXBTC15M-TEST",
                datetime(2026, 5, 15, 11, 45, tzinfo=UTC).isoformat(),
                close_time.isoformat(),
                100_090.0,
                100_000.0,
                100_000.0,
                90.0,
                -2.0,
                1.0,
                1.0,
                1.0,
                1.0,
                0.0,
                0.0,
                "{}",
            ),
        )
    assert "realtime_snapshots_1s" in stream_tables
    assert "predictions" not in stream_tables
    assert "paper_trades" not in stream_tables

    predictions = _ledger_rows(results_db, "predictions")
    assert predictions[0]["action"] == "BUY_YES"
    assert predictions[0]["side"] == "YES"
    assert predictions[0]["strategy"] == "simple_directional"
    assert "btc_velocity_30s" in predictions[0]["features_json"]
    assert "distance_from_strike" in predictions[0]["features_json"]

    trades = _ledger_rows(results_db, "paper_trades")
    assert trades[0]["strategy"] == "simple_directional"
    assert trades[0]["side"] == "YES"
    assert trades[0]["entry_price"] == pytest.approx(0.54)
    assert trades[0]["status"] == "SETTLED"
    assert trades[0]["exit_price"] == pytest.approx(1.0)
    assert trades[0]["realized_pnl"] == pytest.approx((25.0 / 0.54) - 25.0)
    assert trades[0]["exit_reason"] == "1s_expiry_above"


def test_1s_paper_trader_advances_cursor_past_malformed_snapshot(tmp_path: Path) -> None:
    snapshot_db = tmp_path / "snapshots.sqlite3"
    results_db = tmp_path / "paper-results-1s.sqlite3"
    _snapshot_db(
        snapshot_db,
        rows=[
            {
                "ts": _iso(10),
                "market_ticker": "KXBTC15M-BAD",
                "market_close_time": "not-a-timestamp",
                "btc_price": 100_020.0,
                "strike": 100_000.0,
                "btc_velocity_30s": 2.5,
            },
            {
                "ts": _iso(20),
                "market_ticker": "KXBTC15M-GOOD",
                "market_close_time": datetime(2026, 5, 15, 12, 10, tzinfo=UTC).isoformat(),
                "btc_price": 100_020.0,
                "strike": 100_000.0,
                "btc_velocity_30s": 2.5,
            },
        ],
    )

    trader = OneSecondPaperTrader(
        snapshot_db=snapshot_db,
        ledger_db=results_db,
        strategies=[SimpleDirectionalStrategy()],
        risk_limits=RiskLimits(base_size_dollars=25.0, max_position_dollars=25.0, max_spread=0.05),
    )

    first = trader.run_once(limit=1)
    second = trader.run_once(limit=1)

    assert first.skipped_snapshots == 1
    assert second.snapshots_processed == 1
    assert second.signals_recorded == 1
    predictions = _ledger_rows(results_db, "predictions")
    assert [row["market_ticker"] for row in predictions] == ["KXBTC15M-GOOD"]


def test_1s_paper_trader_records_signal_but_blocks_wide_spread_fill(tmp_path: Path) -> None:
    snapshot_db = tmp_path / "snapshots.sqlite3"
    results_db = tmp_path / "paper-results-1s.sqlite3"
    _snapshot_db(
        snapshot_db,
        rows=[
            {
                "ts": _iso(20),
                "market_ticker": "KXBTC15M-WIDE",
                "market_close_time": (datetime(2026, 5, 15, 12, 10, tzinfo=UTC)).isoformat(),
                "btc_price": 100_020.0,
                "strike": 100_000.0,
                "btc_velocity_30s": 2.5,
                "yes_bid": 0.40,
                "yes_ask": 0.54,
            },
        ],
    )

    trader = OneSecondPaperTrader(
        snapshot_db=snapshot_db,
        ledger_db=results_db,
        strategies=[SimpleDirectionalStrategy()],
        risk_limits=RiskLimits(base_size_dollars=25.0, max_position_dollars=25.0, max_spread=0.05),
    )
    summary = trader.run_once(limit=10)

    assert summary.signals_recorded == 1
    assert summary.trades_opened == 0
    assert count_paper_trades(results_db) == 0
    predictions = _ledger_rows(results_db, "predictions")
    assert predictions[0]["action"] == "BUY_YES"
    assert predictions[0]["stake_dollars"] == 0.0
    assert "spread_too_wide" in predictions[0]["reasons_json"]
