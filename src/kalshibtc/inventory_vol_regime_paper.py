from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .execution.inventory_vol_regime import InventoryVolRegimeConfig, InventoryVolRegimeManager
from .market.state import MarketState
from .pair_arb_grid_paper import event_key_for_state
from .storage.paper_signal_store import PaperSignalStore, initialize_results_db


def initialize_inventory_vol_regime_tables(results_db: str | Path) -> None:
    path = Path(results_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS inventory_vol_regime_positions (
                event_key TEXT PRIMARY KEY,
                market_ticker TEXT NOT NULL,
                market_close_time TEXT NOT NULL,
                strike REAL NOT NULL,
                held_above_qty REAL NOT NULL,
                held_above_avg REAL NOT NULL,
                held_below_qty REAL NOT NULL,
                held_below_avg REAL NOT NULL,
                blended_basis REAL,
                total_cost REAL NOT NULL,
                mark_to_market_equity REAL NOT NULL,
                unrealized_pnl REAL,
                inventory_imbalance_ratio REAL NOT NULL,
                inventory_imbalance_qty REAL NOT NULL,
                max_drawdown REAL NOT NULL,
                time_to_expiry REAL NOT NULL,
                distance_from_strike REAL NOT NULL,
                fills_json TEXT NOT NULL,
                equity_curve_json TEXT NOT NULL,
                features_timeline_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS inventory_vol_regime_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                ts TEXT NOT NULL,
                side TEXT NOT NULL,
                event_type TEXT NOT NULL,
                price REAL,
                quantity REAL NOT NULL,
                reason TEXT NOT NULL,
                raw_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS inventory_vol_regime_research_metrics (
                metric_name TEXT PRIMARY KEY,
                metric_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )


class InventoryVolRegimePaperTrader:
    def __init__(self, *, snapshot_db: str | Path, results_db: str | Path, config: InventoryVolRegimeConfig | None = None) -> None:
        self.snapshot_db = Path(snapshot_db)
        self.results_db = Path(results_db)
        self.config = config or InventoryVolRegimeConfig()
        self.manager = InventoryVolRegimeManager(self.config)
        initialize_results_db(self.results_db)
        initialize_inventory_vol_regime_tables(self.results_db)

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
                    btc_price=state.price,
                    btc_velocity_30s=_velocity_from_state(state),
                    above_bid=state.orderbook.yes_bid,
                    above_ask=state.orderbook.yes_ask,
                    below_bid=state.orderbook.no_bid,
                    below_ask=state.orderbook.no_ask,
                    atr_1m=_raw_float(state, "atr_1m"),
                    atr_expansion_rate=_raw_float(state, "atr_expansion_rate"),
                    macd_histogram=_raw_float(state, "macd_histogram"),
                    macd_slope=_raw_float(state, "macd_slope"),
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
                INSERT OR REPLACE INTO inventory_vol_regime_positions (
                    event_key, market_ticker, market_close_time, strike,
                    held_above_qty, held_above_avg, held_below_qty, held_below_avg,
                    blended_basis, total_cost, mark_to_market_equity, unrealized_pnl,
                    inventory_imbalance_ratio, inventory_imbalance_qty, max_drawdown,
                    time_to_expiry, distance_from_strike, fills_json, equity_curve_json,
                    features_timeline_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["event_key"], row["market_ticker"], row["market_close_time"], row["strike"],
                    row["held_above_qty"], row["held_above_avg"], row["held_below_qty"], row["held_below_avg"],
                    row["blended_basis"], row["total_cost"], row["mark_to_market_equity"], row["unrealized_pnl"],
                    row["inventory_imbalance_ratio"], row["inventory_imbalance_qty"], row["max_drawdown"],
                    row["time_to_expiry"], row["distance_from_strike"], json.dumps(row["fills_json"], sort_keys=True),
                    json.dumps(row["equity_curve_json"], sort_keys=True), json.dumps(row["features_timeline_json"], sort_keys=True),
                    (row["equity_curve_json"][-1]["ts"] if row["equity_curve_json"] else ""),
                ),
            )
        for event in self.manager.events:
            conn.execute(
                """
                INSERT INTO inventory_vol_regime_events (
                    event_key, market_ticker, ts, side, event_type, price, quantity, reason, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_key"], event["market_ticker"], event["ts"], event["side"], event["event_type"],
                    event["price"], event["quantity"], event["reason"], json.dumps(event["raw_json"], sort_keys=True),
                ),
            )
        self.manager.events.clear()
        metrics = self.manager.research_metrics()
        updated_at = ""
        equity = metrics.get("mark_to_market_equity_curve") or []
        if equity:
            updated_at = equity[-1]["ts"]
        for name, value in metrics.items():
            conn.execute(
                """
                INSERT OR REPLACE INTO inventory_vol_regime_research_metrics (metric_name, metric_json, updated_at)
                VALUES (?, ?, ?)
                """,
                (name, json.dumps(value, sort_keys=True), updated_at),
            )

    def summary(self) -> dict[str, Any]:
        positions = list(self.manager.positions.values())
        total_cost = sum(position.total_cost for position in positions)
        mtm = sum(position.last_mtm_equity for position in positions)
        metrics = self.manager.research_metrics()
        return {
            "strategy": "inventory_vol_regime",
            "inventory_positions": len(positions),
            "held_above_qty": round(sum(position.held_above_qty for position in positions), 10),
            "held_below_qty": round(sum(position.held_below_qty for position in positions), 10),
            "blended_basis": _avg([position.blended_basis for position in positions if position.blended_basis is not None]),
            "total_cost": round(total_cost, 10),
            "mark_to_market_equity": round(mtm, 10),
            "unrealized_pnl": round(sum((position.equity_curve[-1].get("unrealized_pnl") or 0.0) for position in positions if position.equity_curve), 10),
            "inventory_imbalance_ratio": max((position.inventory_imbalance_ratio for position in positions), default=1.0),
            "inventory_imbalance_qty": round(sum(position.inventory_imbalance_qty for position in positions), 10),
            "largest_drawdown": min((position.max_drawdown for position in positions), default=0.0),
            "research_metrics": metrics,
        }


def _velocity_from_state(state: MarketState) -> float | None:
    return state.slope_30s if state.slope_30s is not None else _raw_float(state, "btc_velocity_30s")


def _raw_float(state: MarketState, key: str) -> float | None:
    value = state.tick.raw.get(key) if state.tick.raw else None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 10) if values else None
