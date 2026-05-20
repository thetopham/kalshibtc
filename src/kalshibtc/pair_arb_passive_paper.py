from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .execution.pair_passive_inventory import PairArbPassiveConfig, PairArbPassiveManager
from .market.state import MarketState
from .storage.paper_signal_store import PaperSignalStore, initialize_results_db


def initialize_pair_passive_tables(results_db: str | Path) -> None:
    path = Path(results_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pair_passive_positions (
                event_key TEXT PRIMARY KEY,
                market_ticker TEXT NOT NULL,
                market_close_time TEXT NOT NULL,
                strike REAL NOT NULL,
                held_above_qty REAL NOT NULL,
                held_above_avg REAL NOT NULL,
                held_below_qty REAL NOT NULL,
                held_below_avg REAL NOT NULL,
                pair_cost REAL,
                locked_profit REAL NOT NULL,
                paired_qty REAL NOT NULL,
                unpaired_directional_exposure REAL NOT NULL,
                total_cost REAL NOT NULL,
                max_capital_at_risk REAL NOT NULL,
                locked_profit_to_max_capital_at_risk REAL NOT NULL,
                time_to_expiry REAL NOT NULL,
                distance_from_strike REAL NOT NULL,
                time_to_first_pair REAL,
                largest_inventory_imbalance REAL NOT NULL DEFAULT 0,
                same_side_fill_streak INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pair_passive_orders (
                id TEXT PRIMARY KEY,
                event_key TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                bid_price REAL NOT NULL,
                quantity REAL NOT NULL,
                created_ts TEXT NOT NULL,
                expires_ts TEXT NOT NULL,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pair_passive_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                event_key TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                quantity REAL NOT NULL,
                cost REAL NOT NULL,
                ts TEXT NOT NULL,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pair_passive_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                event_key TEXT NOT NULL,
                market_ticker TEXT,
                ts TEXT,
                side TEXT,
                reason TEXT,
                raw_json TEXT NOT NULL
            );
            """
        )
        _ensure_column(conn, "pair_passive_positions", "time_to_first_pair", "REAL")
        _ensure_column(conn, "pair_passive_positions", "largest_inventory_imbalance", "REAL NOT NULL DEFAULT 0")
        _ensure_column(conn, "pair_passive_positions", "same_side_fill_streak", "INTEGER NOT NULL DEFAULT 0")


class PairArbPassivePaperTrader:
    def __init__(self, *, snapshot_db: str | Path, results_db: str | Path, config: PairArbPassiveConfig | None = None) -> None:
        self.snapshot_db = Path(snapshot_db)
        self.results_db = Path(results_db)
        self.config = config or PairArbPassiveConfig()
        self.manager = PairArbPassiveManager(self.config)
        initialize_results_db(self.results_db)
        initialize_pair_passive_tables(self.results_db)

    def run_once(self, *, limit: int = 250) -> dict[str, Any]:
        processed = 0
        with PaperSignalStore(snapshot_db=self.snapshot_db, results_db=self.results_db) as store:
            rows = store.select_unprocessed_snapshots(limit=limit)
            for row in rows:
                key = store.snapshot_key(row)
                if store.is_snapshot_processed(key):
                    store.advance_cursor(row)
                    continue
                try:
                    state = store.state_from_snapshot(row)
                except (KeyError, TypeError, ValueError):
                    store.mark_snapshot_processed(key)
                    store.advance_cursor(row)
                    continue
                processed += 1
                self.manager.on_book(
                    event_key=event_key_for_state(state),
                    market_ticker=state.contract.ticker,
                    ts=state.tick.ts,
                    market_close_time=state.contract.close_time.isoformat(),
                    strike=state.strike,
                    seconds_to_close=state.seconds_to_close,
                    distance_from_strike=state.distance_from_strike,
                    above_ask=state.orderbook.yes_ask,
                    below_ask=state.orderbook.no_ask,
                )
                self._persist(store.results)
                store.mark_snapshot_processed(key)
                store.advance_cursor(row)
        summary = self.summary()
        summary.update({"snapshots_processed": processed, "signals_recorded": processed})
        return summary

    def _persist(self, conn: sqlite3.Connection) -> None:
        for position in self.manager.positions.values():
            row = position.summary_dict()
            conn.execute(
                """
                INSERT OR REPLACE INTO pair_passive_positions (
                    event_key, market_ticker, market_close_time, strike,
                    held_above_qty, held_above_avg, held_below_qty, held_below_avg,
                    pair_cost, locked_profit, paired_qty, unpaired_directional_exposure,
                    total_cost, max_capital_at_risk, locked_profit_to_max_capital_at_risk,
                    time_to_expiry, distance_from_strike, time_to_first_pair,
                    largest_inventory_imbalance, same_side_fill_streak, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["event_key"], row["market_ticker"], row["market_close_time"], row["strike"],
                    row["held_above_qty"], row["held_above_avg"], row["held_below_qty"], row["held_below_avg"],
                    row["pair_cost"], row["locked_profit"], row["paired_qty"], row["unpaired_directional_exposure"],
                    row["total_cost"], row["max_capital_at_risk"], row["locked_profit_to_max_capital_at_risk"],
                    row["time_to_expiry"], row["distance_from_strike"], row["time_to_first_pair"],
                    row["largest_inventory_imbalance"], row["same_side_fill_streak"], "",
                ),
            )
        for order in self.manager.orders:
            payload = order.as_dict()
            conn.execute(
                """
                INSERT OR IGNORE INTO pair_passive_orders (
                    id, event_key, market_ticker, side, bid_price, quantity, created_ts, expires_ts, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (order.id, order.event_key, order.market_ticker, order.side, order.bid_price, order.quantity, order.created_ts.isoformat(), order.expires_ts.isoformat(), json.dumps(payload, sort_keys=True)),
            )
        for fill in self.manager.fills:
            payload = fill.as_dict()
            conn.execute(
                """
                INSERT INTO pair_passive_fills (order_id, event_key, market_ticker, side, price, quantity, cost, ts, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (fill.order_id, fill.event_key, fill.market_ticker, fill.side, fill.price, fill.quantity, fill.cost, fill.ts.isoformat(), json.dumps(payload, sort_keys=True)),
            )
        self.manager.fills.clear()
        for event in self.manager.events:
            conn.execute(
                """
                INSERT INTO pair_passive_events (event_type, event_key, market_ticker, ts, side, reason, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (str(event.get("type")), str(event.get("event_key")), event.get("market_ticker"), event.get("ts"), event.get("side"), event.get("reason"), json.dumps(event, sort_keys=True)),
            )
        self.manager.events.clear()

    def summary(self) -> dict[str, Any]:
        positions = list(self.manager.positions.values())
        locked = sum(position.locked_profit for position in positions)
        risk = max((position.max_capital_at_risk for position in positions), default=0.0)
        return {
            "strategy": "pair_arb_passive",
            "pair_positions": len(positions),
            "passive_orders": len(self.manager.orders),
            "passive_fills": _table_count(self.results_db, "pair_passive_fills"),
            "locked_profit": round(locked, 10),
            "max_capital_at_risk": round(risk, 10),
            "locked_profit_to_max_capital_at_risk": round(locked / risk, 10) if risk > 0 else 0.0,
            "unpaired_directional_exposure": round(sum(position.unpaired_directional_exposure for position in positions), 10),
            "held_above_qty": round(sum(position.held_above_qty for position in positions), 10),
            "held_below_qty": round(sum(position.held_below_qty for position in positions), 10),
            "time_to_first_pair": _min_non_null(position.time_to_first_pair for position in positions),
            "largest_inventory_imbalance": round(max((position.largest_inventory_imbalance for position in positions), default=0.0), 10),
            "same_side_fill_streak": max((position.same_side_fill_streak for position in positions), default=0),
        }


def event_key_for_state(state: MarketState) -> str:
    return f"{state.contract.ticker}|{state.contract.close_time.isoformat()}|{state.strike}"


def _table_count(path: Path, table: str) -> int:
    if not path.exists():
        return 0
    with sqlite3.connect(path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return 0
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])



def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _min_non_null(values: Any) -> float | None:
    materialized = [value for value in values if value is not None]
    return round(min(materialized), 10) if materialized else None
