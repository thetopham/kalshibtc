from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .execution.pair_grid_inventory import PairArbGridConfig, PairArbGridManager
from .market.state import MarketState
from .storage.paper_signal_store import PaperSignalStore, initialize_results_db


def initialize_pair_grid_tables(results_db: str | Path) -> None:
    path = Path(results_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pair_grid_positions (
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
                fills_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pair_grid_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                ts TEXT NOT NULL,
                side TEXT NOT NULL,
                projected_pair_cost REAL,
                quantity REAL NOT NULL,
                reason TEXT NOT NULL,
                raw_json TEXT NOT NULL
            );
            """
        )


class PairArbGridPaperTrader:
    def __init__(
        self,
        *,
        snapshot_db: str | Path,
        results_db: str | Path,
        config: PairArbGridConfig | None = None,
    ) -> None:
        self.snapshot_db = Path(snapshot_db)
        self.results_db = Path(results_db)
        self.config = config or PairArbGridConfig()
        self.manager = PairArbGridManager(self.config)
        initialize_results_db(self.results_db)
        initialize_pair_grid_tables(self.results_db)

    def run_once(self, *, limit: int = 250) -> dict[str, Any]:
        processed = fills = 0
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
                decision = self.manager.on_book(
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
                fills += decision.fills
                self._persist(store.results)
                store.mark_snapshot_processed(key)
                store.advance_cursor(row)
        summary = self.summary()
        summary.update({"snapshots_processed": processed, "signals_recorded": processed, "fills": fills})
        return summary

    def _persist(self, conn: sqlite3.Connection) -> None:
        for position in self.manager.positions.values():
            row = position.summary_dict()
            conn.execute(
                """
                INSERT OR REPLACE INTO pair_grid_positions (
                    event_key, market_ticker, market_close_time, strike,
                    held_above_qty, held_above_avg, held_below_qty, held_below_avg,
                    pair_cost, locked_profit, paired_qty, unpaired_directional_exposure,
                    total_cost, max_capital_at_risk, locked_profit_to_max_capital_at_risk,
                    time_to_expiry, distance_from_strike, fills_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["event_key"], row["market_ticker"], row["market_close_time"], row["strike"],
                    row["held_above_qty"], row["held_above_avg"], row["held_below_qty"], row["held_below_avg"],
                    row["pair_cost"], row["locked_profit"], row["paired_qty"], row["unpaired_directional_exposure"],
                    row["total_cost"], row["max_capital_at_risk"], row["locked_profit_to_max_capital_at_risk"],
                    row["time_to_expiry"], row["distance_from_strike"], json.dumps(row["fills_json"], sort_keys=True),
                    row["fills_json"][-1]["ts"] if row["fills_json"] else "",
                ),
            )
        for event in self.manager.events:
            conn.execute(
                """
                INSERT INTO pair_grid_events (
                    event_key, market_ticker, ts, side, projected_pair_cost, quantity, reason, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_key"], event["market_ticker"], event["ts"], event["side"],
                    event["projected_pair_cost"], event["quantity"], event["reason"], json.dumps(event, sort_keys=True),
                ),
            )
        self.manager.events.clear()

    def summary(self) -> dict[str, Any]:
        positions = list(self.manager.positions.values())
        return {
            "strategy": "pair_arb_grid",
            "pair_positions": len(positions),
            "locked_profit": round(sum(position.locked_profit for position in positions), 10),
            "max_capital_at_risk": round(max((position.max_capital_at_risk for position in positions), default=0.0), 10),
            "locked_profit_to_max_capital_at_risk": _ratio(
                sum(position.locked_profit for position in positions),
                max((position.max_capital_at_risk for position in positions), default=0.0),
            ),
            "unpaired_directional_exposure": round(sum(position.unpaired_directional_exposure for position in positions), 10),
            "held_above_qty": round(sum(position.held_above_qty for position in positions), 10),
            "held_below_qty": round(sum(position.held_below_qty for position in positions), 10),
            "avg_pair_cost": _avg([position.pair_cost for position in positions if position.pair_cost is not None]),
        }


def event_key_for_state(state: MarketState) -> str:
    return f"{state.contract.ticker}|{state.contract.close_time.isoformat()}|{state.strike}"


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 10) if values else None


def _ratio(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 10) if denominator > 0 else 0.0
