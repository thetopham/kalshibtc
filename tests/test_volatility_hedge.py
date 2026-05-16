from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kalshibtc.execution.volatility_hedge import (
    VolatilityHedgeConfig,
    VolatilityHedgeManager,
    VolatilityHedgePosition,
)
from kalshibtc.strategy.registry import create_strategy, strategy_names
from kalshibtc.volatility_hedge_paper import VolatilityHedgePaperTrader


def _ts(offset: int = 0) -> datetime:
    return datetime(2026, 1, 1, 0, 0, tzinfo=UTC) + timedelta(seconds=offset)


def _manager(**kwargs: float) -> VolatilityHedgeManager:
    return VolatilityHedgeManager(VolatilityHedgeConfig(**kwargs))


def _snapshot_db(path: Path, *, rows: list[dict[str, Any]]) -> None:
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
                "market_open_time": _ts(-900).isoformat(),
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


def test_paired_cost_edge_and_equal_settlement_guarantee() -> None:
    pos = VolatilityHedgePosition("event", "ticker", "close", 100_000)
    features = _manager()._features("event", _ts(), 100_010, 800, 10, 1, 5, 0.44, 0.45, 0.53, 0.54)
    pos.add_fill(side="UP", price=0.55, qty=100, fee=0, ts=_ts(), reason="seed", projected_paired_cost=None, features=features)
    pos.add_fill(side="DOWN", price=0.40, qty=100, fee=0, ts=_ts(), reason="hedge", projected_paired_cost=0.95, features=features)

    assert pos.up_avg_entry == 0.55
    assert pos.down_avg_entry == 0.40
    assert pos.paired_qty == 100
    assert pos.paired_cost == 0.95
    assert pos.edge == 0.05
    assert pos.locked_payout == 100
    assert pos.locked_edge_dollars == 5


def test_add_rejects_when_projected_paired_cost_does_not_improve() -> None:
    manager = _manager(slippage=0, fee_per_contract=0, cooldown_seconds=0)
    decision = manager.on_book(
        event_key="event",
        market_ticker="ticker",
        ts=_ts(),
        market_close_time="close",
        strike=100_000,
        seconds_to_close=800,
        distance_from_strike=25,
        btc_price=100_025,
        slope=2,
        up_bid=0.60,
        up_ask=0.61,
        down_bid=0.38,
        down_ask=0.39,
        atr=10,
    )
    assert decision.fills == 2
    current = manager.positions["event"].paired_cost
    decision = manager.on_book(
        event_key="event",
        market_ticker="ticker",
        ts=_ts(11),
        market_close_time="close",
        strike=100_000,
        seconds_to_close=789,
        distance_from_strike=30,
        btc_price=100_030,
        slope=2,
        up_bid=0.60,
        up_ask=0.61,
        down_bid=0.42,
        down_ask=0.43,
        atr=10,
    )
    assert decision.fills == 0
    assert decision.reason == "projected_paired_cost_not_improved"
    assert manager.positions["event"].paired_cost == current


def test_adds_cheaper_side_only_when_projected_paired_cost_improves() -> None:
    manager = _manager(slippage=0, fee_per_contract=0, cooldown_seconds=0, min_paired_cost_improvement=0.001, max_imbalance_ratio=20.0)
    manager.on_book(
        event_key="event",
        market_ticker="ticker",
        ts=_ts(),
        market_close_time="close",
        strike=100_000,
        seconds_to_close=800,
        distance_from_strike=25,
        btc_price=100_025,
        slope=2,
        up_bid=0.60,
        up_ask=0.61,
        down_bid=0.38,
        down_ask=0.39,
        atr=10,
    )
    before = manager.positions["event"].paired_cost
    decision = manager.on_book(
        event_key="event",
        market_ticker="ticker",
        ts=_ts(11),
        market_close_time="close",
        strike=100_000,
        seconds_to_close=789,
        distance_from_strike=60,
        btc_price=100_060,
        slope=5,
        up_bid=0.78,
        up_ask=0.79,
        down_bid=0.14,
        down_ask=0.15,
        atr=30,
    )
    after = manager.positions["event"].paired_cost
    assert decision.fills == 1
    assert decision.fill_events[0].side == "DOWN"
    assert after is not None and before is not None and after < before
    assert after < 0.98


def test_invalid_book_rejected_and_no_position_created() -> None:
    manager = _manager()
    decision = manager.on_book(
        event_key="event",
        market_ticker="ticker",
        ts=_ts(),
        market_close_time="close",
        strike=100_000,
        seconds_to_close=800,
        distance_from_strike=0,
        btc_price=100_000,
        slope=0,
        up_bid=0.70,
        up_ask=0.60,
        down_bid=0.30,
        down_ask=0.40,
    )
    assert decision.fills == 0
    assert decision.reason == "invalid_book"
    assert manager.positions == {}


def test_strategy_registry_contains_paper_only_volatility_hedge() -> None:
    assert "volatility_hedge" in strategy_names()
    strategy = create_strategy("volatility_hedge")
    assert strategy.name == "volatility_hedge"


def test_paper_trader_initializes_only_volatility_hedge_tables(tmp_path: Path) -> None:
    snapshot_db = tmp_path / "snapshots.sqlite3"
    results_db = tmp_path / "results.sqlite3"
    trader = VolatilityHedgePaperTrader(snapshot_db=snapshot_db, results_db=results_db)
    assert trader.summary()["strategy"] == "volatility_hedge"
    with sqlite3.connect(results_db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "volatility_hedge_positions" in tables
    assert "volatility_hedge_events" in tables
    assert "orders" not in tables


def test_paper_trader_skips_invalid_event_key_without_position_or_fill(tmp_path: Path) -> None:
    snapshot_db = tmp_path / "snapshots.sqlite3"
    results_db = tmp_path / "results.sqlite3"
    _snapshot_db(
        snapshot_db,
        rows=[
            {
                "ts": _ts().isoformat(),
                "market_ticker": "KXBTC15M-26MAY161130-30",
                "market_close_time": _ts(900).isoformat(),
                "btc_price": 100_020.0,
                "strike": 15.0,
                "target_price": 15.0,
                "seconds_to_close": 900.0,
                "btc_velocity_30s": 1.5,
            }
        ],
    )

    trader = VolatilityHedgePaperTrader(snapshot_db=snapshot_db, results_db=results_db)
    summary = trader.run_once(limit=10)

    assert summary["snapshots_processed"] == 1
    assert summary["batch_fills"] == 0
    assert summary["volatility_hedge_positions"] == 0
    with sqlite3.connect(results_db) as conn:
        conn.row_factory = sqlite3.Row
        events = conn.execute("SELECT * FROM volatility_hedge_events").fetchall()
        positions = conn.execute("SELECT COUNT(*) FROM volatility_hedge_positions").fetchone()[0]
        fills = conn.execute(
            "SELECT COUNT(*) FROM volatility_hedge_events WHERE event_type = 'fill'"
        ).fetchone()[0]
        paper_trades = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]

    assert positions == 0
    assert fills == 0
    assert paper_trades == 0
    assert len(events) == 1
    assert events[0]["reason"] in {"invalid_event_key", "skipped_invalid_event_key"}
    assert events[0]["side"] == "NONE"
    assert events[0]["qty"] == 0
    assert events[0]["allowed"] == 0


def test_paper_trader_reloads_existing_positions_and_reports_cumulative_metrics(tmp_path: Path) -> None:
    snapshot_db = tmp_path / "snapshots.sqlite3"
    results_db = tmp_path / "results.sqlite3"
    first = VolatilityHedgePaperTrader(snapshot_db=snapshot_db, results_db=results_db)
    features = _manager()._features("event", _ts(), 100_010, 800, 10, 1, 5, 0.44, 0.45, 0.53, 0.54)
    position = first.manager.positions.setdefault("event", VolatilityHedgePosition("event", "ticker", _ts(-1).isoformat(), 100_000))
    position.add_fill(side="UP", price=0.45, qty=10, fee=0, ts=_ts(), reason="seed", projected_paired_cost=None, features=features)
    position.add_fill(side="DOWN", price=0.50, qty=10, fee=0, ts=_ts(), reason="seed", projected_paired_cost=0.95, features=features)
    first.manager.events.extend([
        {"event_key": "event", "market_ticker": "ticker", "ts": _ts().isoformat(), "side": "UP", "event_type": "fill", "reason": "seed", "price": 0.45, "qty": 10, "projected_paired_cost": None, "current_paired_cost": None, "edge": None, "up_qty": 10, "down_qty": 0, "time_to_expiry": 800, "slope": 1, "atr": 5, "distance_from_strike": 10, "up_bid": 0.44, "up_ask": 0.45, "down_bid": 0.53, "down_ask": 0.54, "raw_json": {}},
        {"event_key": "event", "market_ticker": "ticker", "ts": _ts().isoformat(), "side": "DOWN", "event_type": "fill", "reason": "seed", "price": 0.50, "qty": 10, "projected_paired_cost": 0.95, "current_paired_cost": 0.45, "edge": 0.05, "up_qty": 10, "down_qty": 10, "time_to_expiry": 800, "slope": 1, "atr": 5, "distance_from_strike": 10, "up_bid": 0.44, "up_ask": 0.45, "down_bid": 0.49, "down_ask": 0.50, "raw_json": {}},
    ])
    with sqlite3.connect(results_db) as conn:
        conn.row_factory = sqlite3.Row
        first._persist(conn)
        conn.execute("INSERT INTO processed_snapshots (ts, market_ticker, processed_at) VALUES ('t1', 'ticker', 'now')")
        conn.execute("CREATE TABLE realtime_snapshots_1s (ts TEXT, market_ticker TEXT, btc_price REAL, strike REAL)")
        conn.execute("INSERT INTO realtime_snapshots_1s VALUES (?, 'ticker', 100010, 100000)", (_ts(-1).isoformat(),))

    second = VolatilityHedgePaperTrader(snapshot_db=snapshot_db, results_db=results_db)
    assert second.manager.positions["event"].up_qty == 10
    assert second.manager.positions["event"].down_qty == 10
    metrics = second._cumulative_metrics()
    assert metrics["snapshots"] == 1
    assert metrics["signals"] == 1
    assert metrics["fills"] == 2
    assert metrics["notional"] == 9.5
    assert metrics["settled_positions"] == 1
    assert metrics["win_rate"] == 1.0
    assert metrics["ev_per_trade"] == 0.5
