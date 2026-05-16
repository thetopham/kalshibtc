from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .execution.pair_inventory import HedgeManager, InventoryManager, LegSide, PairArbConfig
from .market.state import MarketState
from .storage.paper_signal_store import PaperSignalStore
from .storage.paper_signal_store import initialize_results_db
from .strategy.signals import Signal
from .strategy.pair_arb import PairArbStrategy


def initialize_pair_tables(results_db: str | Path) -> None:
    path = Path(results_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pair_positions (
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
                unpaired_side TEXT,
                time_to_expiry REAL NOT NULL,
                distance_from_strike REAL NOT NULL,
                fills_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS hedge_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                ts TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                quantity REAL NOT NULL,
                fee REAL NOT NULL,
                pair_cost REAL,
                locked_profit_after REAL,
                reason TEXT NOT NULL,
                raw_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS missed_hedge_opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                ts TEXT NOT NULL,
                held_side TEXT,
                opposite_side TEXT,
                opposite_ask REAL,
                projected_pair_cost REAL,
                threshold REAL NOT NULL,
                reason TEXT NOT NULL,
                seconds_to_close REAL NOT NULL,
                distance_from_strike REAL NOT NULL,
                raw_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rejected_seed_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                ts TEXT NOT NULL,
                side TEXT NOT NULL,
                ask_price REAL NOT NULL,
                effective_price REAL NOT NULL,
                quantity REAL NOT NULL,
                threshold REAL NOT NULL,
                opposite_ask REAL,
                required_opposite_ask REAL NOT NULL,
                reason TEXT NOT NULL,
                seconds_to_close REAL NOT NULL,
                distance_from_strike REAL NOT NULL,
                raw_json TEXT NOT NULL
            );
            """
        )


class PairArbPaperTrader:
    """Paper-only paired-leg executor using the recorder feed as a read-only tape."""

    def __init__(
        self,
        *,
        snapshot_db: str | Path,
        results_db: str | Path,
        config: PairArbConfig | None = None,
        strategy: PairArbStrategy | None = None,
    ) -> None:
        self.snapshot_db = Path(snapshot_db)
        self.results_db = Path(results_db)
        self.config = config or PairArbConfig()
        self.strategy = strategy or PairArbStrategy()
        self.inventory = InventoryManager(self.config)
        self.hedges = HedgeManager(self.config)
        initialize_results_db(self.results_db)
        initialize_pair_tables(self.results_db)

    def run_once(self, *, limit: int = 250) -> dict[str, Any]:
        processed = signals = initial_fills = hedge_fills = 0
        with PaperSignalStore(snapshot_db=self.snapshot_db, results_db=self.results_db) as store:
            self._load_existing_positions(store.results)
            rows = store.select_unprocessed_snapshots(limit=limit)
            for row in rows:
                snapshot_key = store.snapshot_key(row)
                if store.is_snapshot_processed(snapshot_key):
                    store.advance_cursor(row)
                    continue
                try:
                    state = store.state_from_snapshot(row)
                except (KeyError, TypeError, ValueError):
                    store.mark_snapshot_processed(snapshot_key)
                    store.advance_cursor(row)
                    continue
                processed += 1
                signal = self.strategy.on_tick(state)
                signals += 1
                event_key = event_key_for_state(state)
                position = self.inventory.positions.get(event_key)
                if position is None and signal.side in {"long_above", "long_below"}:
                    side: LegSide = "ABOVE" if signal.side == "long_above" else "BELOW"
                    ask = ask_for_side(state, side)
                    if ask is not None and valid_book(state):
                        opposite_ask = ask_for_side(state, "BELOW" if side == "ABOVE" else "ABOVE")
                        result = self.inventory.buy_initial_leg(
                            event_key=event_key,
                            market_ticker=state.contract.ticker,
                            side=side,
                            ask_price=ask,
                            quantity=self.config.initial_qty,
                            ts=state.tick.ts,
                            market_close_time=state.contract.close_time.isoformat(),
                            strike=state.strike,
                            distance_from_strike=state.distance_from_strike,
                            seconds_to_close=state.seconds_to_close,
                            opposite_ask=opposite_ask,
                        )
                        if result.filled:
                            initial_fills += 1
                else:
                    position = self.inventory.positions.get(event_key)
                    if position is not None and valid_book(state):
                        opposite = "BELOW" if position.unpaired_side == "ABOVE" else "ABOVE" if position.unpaired_side == "BELOW" else None
                        opposite_ask = ask_for_side(state, opposite) if opposite else None
                        result = self.hedges.evaluate_and_fill(
                            inventory=self.inventory,
                            event_key=event_key,
                            opposite_ask=opposite_ask,
                            ts=state.tick.ts,
                            distance_from_strike=state.distance_from_strike,
                            seconds_to_close=state.seconds_to_close,
                        )
                        if result.filled:
                            hedge_fills += 1
                self._persist(store.results)
                store.mark_snapshot_processed(snapshot_key)
                store.advance_cursor(row)
        summary = self.summary()
        summary.update(
            {
                "snapshots_processed": processed,
                "signals_recorded": signals,
                "initial_inventory_fills": initial_fills,
                "hedge_fills": hedge_fills,
            }
        )
        return summary

    def _load_existing_positions(self, conn: sqlite3.Connection) -> None:
        if not _table_exists(conn, "pair_positions"):
            return
        rows = conn.execute("SELECT * FROM pair_positions").fetchall()
        for row in rows:
            # Existing position reconstruction is intentionally conservative for loop continuity.
            # Full fill history is in fills_json; rebuild weighted inventory from those fills.
            from .execution.pair_inventory import PairPosition

            position = PairPosition(
                event_key=str(row["event_key"]),
                market_ticker=str(row["market_ticker"]),
                market_close_time=str(row["market_close_time"]),
                strike=float(row["strike"]),
                last_seconds_to_close=float(row["time_to_expiry"]),
                last_distance_from_strike=float(row["distance_from_strike"]),
            )
            try:
                fills = json.loads(str(row["fills_json"] or "[]"))
            except json.JSONDecodeError:
                fills = []
            for fill in fills:
                position.add_fill(
                    side=str(fill["side"]),
                    price=float(fill["price"]),
                    quantity=float(fill["quantity"]),
                    fee=float(fill.get("fee") or 0.0),
                    ts=_parse_ts(str(fill["ts"])),
                    reason=str(fill.get("reason") or "restored"),
                )
            self.inventory.positions[position.event_key] = position

    def _persist(self, conn: sqlite3.Connection) -> None:
        for position in self.inventory.positions.values():
            row = position.summary_dict()
            conn.execute(
                """
                INSERT OR REPLACE INTO pair_positions (
                    event_key, market_ticker, market_close_time, strike,
                    held_above_qty, held_above_avg, held_below_qty, held_below_avg,
                    pair_cost, locked_profit, paired_qty, unpaired_directional_exposure,
                    unpaired_side, time_to_expiry, distance_from_strike, fills_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["event_key"],
                    row["market_ticker"],
                    row["market_close_time"],
                    row["strike"],
                    row["held_above_qty"],
                    row["held_above_avg"],
                    row["held_below_qty"],
                    row["held_below_avg"],
                    row["pair_cost"],
                    row["locked_profit"],
                    row["paired_qty"],
                    row["unpaired_directional_exposure"],
                    row["unpaired_side"],
                    row["time_to_expiry"],
                    row["distance_from_strike"],
                    json.dumps(row["fills_json"], sort_keys=True),
                    position.last_ts.isoformat() if position.last_ts else "",
                ),
            )
        for result in self.inventory.hedge_events:
            if result.fill is None:
                continue
            fill = result.fill
            conn.execute(
                """
                INSERT INTO hedge_events (
                    event_key, market_ticker, ts, side, price, quantity, fee,
                    pair_cost, locked_profit_after, reason, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.event_key,
                    fill.market_ticker,
                    fill.ts.isoformat(),
                    fill.side,
                    fill.price,
                    fill.quantity,
                    fill.fee,
                    result.pair_cost,
                    result.locked_profit_after,
                    result.reason,
                    json.dumps(fill.as_dict(), sort_keys=True),
                ),
            )
        self.inventory.hedge_events.clear()
        for missed in self.inventory.missed_hedges:
            payload = missed.as_dict()
            conn.execute(
                """
                INSERT INTO missed_hedge_opportunities (
                    event_key, market_ticker, ts, held_side, opposite_side, opposite_ask,
                    projected_pair_cost, threshold, reason, seconds_to_close,
                    distance_from_strike, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["event_key"],
                    payload["market_ticker"],
                    payload["ts"],
                    payload["held_side"],
                    payload["opposite_side"],
                    payload["opposite_ask"],
                    payload["projected_pair_cost"],
                    payload["threshold"],
                    payload["reason"],
                    payload["seconds_to_close"],
                    payload["distance_from_strike"],
                    json.dumps(payload, sort_keys=True),
                ),
            )
        self.inventory.missed_hedges.clear()
        for rejected in self.inventory.rejected_seed_attempts:
            payload = rejected.as_dict()
            conn.execute(
                """
                INSERT INTO rejected_seed_attempts (
                    event_key, market_ticker, ts, side, ask_price, effective_price,
                    quantity, threshold, opposite_ask, required_opposite_ask, reason,
                    seconds_to_close, distance_from_strike, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["event_key"], payload["market_ticker"], payload["ts"], payload["side"],
                    payload["ask_price"], payload["effective_price"], payload["quantity"], payload["threshold"],
                    payload["opposite_ask"], payload["required_opposite_ask"], payload["reason"],
                    payload["seconds_to_close"], payload["distance_from_strike"], json.dumps(payload, sort_keys=True),
                ),
            )
        self.inventory.rejected_seed_attempts.clear()

    def summary(self) -> dict[str, Any]:
        positions = list(self.inventory.positions.values())
        return {
            "strategy": "pair_arb",
            "pair_positions": len(positions),
            "hedge_events": sum(1 for position in positions if position.paired_qty > 0),
            "missed_hedge_events": _table_count(self.results_db, "missed_hedge_opportunities"),
            "rejected_seed_attempts": _table_count(self.results_db, "rejected_seed_attempts"),
            "locked_profit": round(sum(position.locked_profit for position in positions), 10),
            "unpaired_directional_exposure": round(sum(position.unpaired_directional_exposure for position in positions), 10),
            "held_above_qty": round(sum(position.held_above_qty for position in positions), 10),
            "held_below_qty": round(sum(position.held_below_qty for position in positions), 10),
            "avg_pair_cost": _avg([position.pair_cost for position in positions if position.pair_cost is not None]),
        }


def event_key_for_state(state: MarketState) -> str:
    return f"{state.contract.ticker}|{state.contract.close_time.isoformat()}|{state.strike}"


def ask_for_side(state: MarketState, side: LegSide | None) -> float | None:
    if side == "ABOVE":
        return state.orderbook.yes_ask
    if side == "BELOW":
        return state.orderbook.no_ask
    return None


def valid_book(state: MarketState) -> bool:
    book = state.orderbook
    prices = (book.yes_bid, book.yes_ask, book.no_bid, book.no_ask)
    if any(price is None for price in prices):
        return False
    yes_bid, yes_ask, no_bid, no_ask = prices
    assert yes_bid is not None and yes_ask is not None and no_bid is not None and no_ask is not None
    if any(price <= 0 or price > 1 for price in (yes_bid, yes_ask, no_bid, no_ask)):
        return False
    if yes_bid > yes_ask or no_bid > no_ask:
        return False
    if yes_bid + no_bid > 1.0:
        return False
    return True


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _parse_ts(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 10) if values else None


def _table_count(path: Path, table: str) -> int:
    if not path.exists():
        return 0
    with sqlite3.connect(path) as conn:
        if not _table_exists(conn, table):
            return 0
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
