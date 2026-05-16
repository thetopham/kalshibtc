from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from .execution.volatility_hedge import (
    VolatilityHedgeConfig,
    VolatilityHedgeManager,
    event_key_for_volatility_hedge,
    position_from_summary,
)
from .market.state import MarketState
from .storage.paper_signal_store import PaperSignalStore, initialize_results_db


def initialize_volatility_hedge_tables(results_db: str | Path) -> None:
    path = Path(results_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS volatility_hedge_positions (
                event_key TEXT PRIMARY KEY,
                market_ticker TEXT NOT NULL,
                market_close_time TEXT NOT NULL,
                strike REAL NOT NULL,
                up_qty REAL NOT NULL,
                down_qty REAL NOT NULL,
                up_avg_entry REAL NOT NULL,
                down_avg_entry REAL NOT NULL,
                paired_qty REAL NOT NULL,
                paired_cost REAL,
                edge REAL,
                locked_payout REAL NOT NULL,
                locked_edge_dollars REAL NOT NULL,
                imbalance_ratio REAL NOT NULL,
                total_cost REAL NOT NULL,
                mode TEXT,
                target_ratio REAL,
                larger_side TEXT,
                smaller_side TEXT,
                repair_qty_needed REAL,
                p_up REAL,
                p_down REAL,
                expected_settlement_value REAL,
                settlement_EV REAL,
                EV_per_dollar REAL,
                up_win_pnl REAL,
                down_win_pnl REAL,
                worst_case_pnl REAL,
                best_case_pnl REAL,
                locked_edge REAL,
                residual_up_qty REAL,
                residual_down_qty REAL,
                residual_EV REAL,
                max_market_notional REAL,
                notional_used REAL,
                notional_remaining REAL,
                lifecycle_phase TEXT,
                elapsed_seconds REAL,
                last_features_json TEXT NOT NULL,
                fills_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS volatility_hedge_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                ts TEXT NOT NULL,
                side TEXT NOT NULL,
                event_type TEXT NOT NULL,
                reason TEXT NOT NULL,
                price REAL,
                qty REAL NOT NULL,
                projected_paired_cost REAL,
                current_paired_cost REAL,
                edge REAL,
                up_qty REAL NOT NULL,
                down_qty REAL NOT NULL,
                time_to_expiry REAL,
                slope REAL,
                atr REAL,
                distance_from_strike REAL,
                up_bid REAL,
                up_ask REAL,
                down_bid REAL,
                down_ask REAL,
                simple_ask_sum REAL,
                require_seed_pair_cost_below REAL,
                max_initial_ask_sum REAL,
                seed_gate_failed_reason TEXT,
                lifecycle_phase TEXT,
                elapsed_seconds REAL,
                phase_seed_pair_threshold REAL,
                phase_max_initial_ask_sum REAL,
                seed_window_open INTEGER,
                normal_add_window_open INTEGER,
                repair_only INTEGER,
                raw_json TEXT NOT NULL
            );
            """
        )
        _ensure_columns(conn, "volatility_hedge_positions", {
            "mode": "TEXT", "target_ratio": "REAL", "larger_side": "TEXT", "smaller_side": "TEXT",
            "repair_qty_needed": "REAL", "p_up": "REAL", "p_down": "REAL", "expected_settlement_value": "REAL",
            "settlement_EV": "REAL", "EV_per_dollar": "REAL", "up_win_pnl": "REAL", "down_win_pnl": "REAL",
            "worst_case_pnl": "REAL", "best_case_pnl": "REAL", "locked_edge": "REAL", "residual_up_qty": "REAL",
            "residual_down_qty": "REAL", "residual_EV": "REAL", "max_market_notional": "REAL", "notional_used": "REAL",
            "notional_remaining": "REAL", "lifecycle_phase": "TEXT", "elapsed_seconds": "REAL",
        })
        _ensure_columns(conn, "volatility_hedge_events", {
            "mode": "TEXT", "allowed": "INTEGER", "proposed_qty": "REAL", "final_qty": "REAL",
            "current_imbalance_ratio": "REAL", "projected_imbalance_ratio": "REAL", "current_settlement_EV": "REAL",
            "projected_settlement_EV": "REAL", "current_worst_case_pnl": "REAL", "projected_worst_case_pnl": "REAL",
            "simple_ask_sum": "REAL", "require_seed_pair_cost_below": "REAL", "max_initial_ask_sum": "REAL",
            "seed_gate_failed_reason": "TEXT", "lifecycle_phase": "TEXT", "elapsed_seconds": "REAL",
            "phase_seed_pair_threshold": "REAL", "phase_max_initial_ask_sum": "REAL", "seed_window_open": "INTEGER",
            "normal_add_window_open": "INTEGER", "repair_only": "INTEGER",
        })


class VolatilityHedgePaperTrader:
    def __init__(self, *, snapshot_db: str | Path, results_db: str | Path, config: VolatilityHedgeConfig | None = None) -> None:
        self.snapshot_db = Path(snapshot_db)
        self.results_db = Path(results_db)
        self.config = config or VolatilityHedgeConfig()
        self.manager = VolatilityHedgeManager(self.config)
        initialize_results_db(self.results_db)
        initialize_volatility_hedge_tables(self.results_db)
        self._load_existing_positions()

    def _load_existing_positions(self) -> None:
        if not self.results_db.exists():
            return
        with sqlite3.connect(f"file:{self.results_db}?mode=ro", uri=True, timeout=2.0) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='volatility_hedge_positions'").fetchone():
                return
            rows = conn.execute("SELECT * FROM volatility_hedge_positions").fetchall()
        self.manager.positions = {str(row["event_key"]): position_from_summary(dict(row)) for row in rows}

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
                market_close_time = state.contract.close_time.isoformat()
                try:
                    event_key = event_key_for_volatility_hedge(state.contract.ticker, market_close_time, state.strike)
                except ValueError as exc:
                    self._record_invalid_event_key(store.results, state, exc)
                    store.mark_snapshot_processed(key)
                    store.advance_cursor(row)
                    continue
                decision = self.manager.on_book(
                    event_key=event_key,
                    market_ticker=state.contract.ticker,
                    ts=state.tick.ts,
                    market_close_time=market_close_time,
                    strike=state.strike,
                    seconds_to_close=state.seconds_to_close,
                    distance_from_strike=state.distance_from_strike,
                    btc_price=state.price,
                    slope=_slope_from_state(state),
                    up_bid=state.orderbook.yes_bid,
                    up_ask=state.orderbook.yes_ask,
                    down_bid=state.orderbook.no_bid,
                    down_ask=state.orderbook.no_ask,
                    atr=_raw_float(state, "atr_1m"),
                )
                fills += decision.fills
                self._persist(store.results)
                store.mark_snapshot_processed(key)
                store.advance_cursor(row)
        summary = self.summary()
        cumulative = self._cumulative_metrics()
        summary.update(cumulative)
        summary.update({"snapshots_processed": processed, "signals_recorded": processed, "batch_fills": fills})
        return summary

    def _record_invalid_event_key(self, conn: sqlite3.Connection, state: MarketState, error: ValueError) -> None:
        market_close_time = state.contract.close_time.isoformat()
        features = {
            "time_to_expiry": state.seconds_to_close,
            "slope": _slope_from_state(state),
            "atr": _raw_float(state, "atr_1m"),
            "distance_from_strike": state.distance_from_strike,
            "up_bid": state.orderbook.yes_bid,
            "up_ask": state.orderbook.yes_ask,
            "down_bid": state.orderbook.no_bid,
            "down_ask": state.orderbook.no_ask,
        }
        self.manager.events.append(
            {
                "event_key": f"invalid_event_key|{state.contract.ticker}|{market_close_time}|{round(float(state.strike), 2)}",
                "market_ticker": state.contract.ticker,
                "ts": state.tick.ts.isoformat(),
                "side": "NONE",
                "event_type": "decision",
                "reason": "invalid_event_key",
                "price": None,
                "qty": 0.0,
                "projected_paired_cost": None,
                "current_paired_cost": None,
                "edge": None,
                "up_qty": 0.0,
                "down_qty": 0.0,
                "time_to_expiry": features["time_to_expiry"],
                "slope": features["slope"],
                "atr": features["atr"],
                "distance_from_strike": features["distance_from_strike"],
                "up_bid": features["up_bid"],
                "up_ask": features["up_ask"],
                "down_bid": features["down_bid"],
                "down_ask": features["down_ask"],
                "mode": None,
                "allowed": False,
                "proposed_qty": 0.0,
                "final_qty": 0.0,
                "current_imbalance_ratio": None,
                "projected_imbalance_ratio": None,
                "current_settlement_EV": None,
                "projected_settlement_EV": None,
                "current_worst_case_pnl": None,
                "projected_worst_case_pnl": None,
                "raw_json": {
                    "error": str(error),
                    "fill": None,
                    "features": features,
                    "balance": {"allowed": False, "reason": "invalid_event_key", "final_qty": 0.0},
                    "market_close_time": market_close_time,
                    "strike": state.strike,
                },
            }
        )
        self._persist_events(conn)

    def _persist(self, conn: sqlite3.Connection) -> None:
        for position in self.manager.positions.values():
            row = position.summary_dict(self.config)
            conn.execute(
                """
                INSERT OR REPLACE INTO volatility_hedge_positions (
                    event_key, market_ticker, market_close_time, strike,
                    up_qty, down_qty, up_avg_entry, down_avg_entry,
                    paired_qty, paired_cost, edge, locked_payout, locked_edge_dollars,
                    imbalance_ratio, total_cost, mode, target_ratio, larger_side, smaller_side,
                    repair_qty_needed, p_up, p_down, expected_settlement_value, settlement_EV,
                    EV_per_dollar, up_win_pnl, down_win_pnl, worst_case_pnl, best_case_pnl,
                    locked_edge, residual_up_qty, residual_down_qty, residual_EV, max_market_notional,
                    notional_used, notional_remaining, lifecycle_phase, elapsed_seconds,
                    last_features_json, fills_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["event_key"], row["market_ticker"], row["market_close_time"], row["strike"],
                    row["up_qty"], row["down_qty"], row["up_avg_entry"], row["down_avg_entry"],
                    row["paired_qty"], row["paired_cost"], row["edge"], row["locked_payout"], row["locked_edge_dollars"],
                    row["imbalance_ratio"], row["total_cost"], row["mode"], row["target_ratio"], row["larger_side"], row["smaller_side"],
                    row["repair_qty_needed"], row["p_up"], row["p_down"], row["expected_settlement_value"], row["settlement_EV"],
                    row["EV_per_dollar"], row["up_win_pnl"], row["down_win_pnl"], row["worst_case_pnl"], row["best_case_pnl"],
                    row["locked_edge"], row["residual_up_qty"], row["residual_down_qty"], row["residual_EV"], row["max_market_notional"],
                    row["notional_used"], row["notional_remaining"], str(row["lifecycle_phase"]), row["elapsed_seconds"],
                    json.dumps(row["last_features_json"], sort_keys=True),
                    json.dumps(row["fills_json"], sort_keys=True), row["last_ts"] or "",
                ),
            )
        self._persist_events(conn)

    def _persist_events(self, conn: sqlite3.Connection) -> None:
        for event in self.manager.events:
            conn.execute(
                """
                INSERT INTO volatility_hedge_events (
                    event_key, market_ticker, ts, side, event_type, reason, price, qty,
                    projected_paired_cost, current_paired_cost, edge, up_qty, down_qty,
                    time_to_expiry, slope, atr, distance_from_strike,
                    up_bid, up_ask, down_bid, down_ask, simple_ask_sum,
                    require_seed_pair_cost_below, max_initial_ask_sum, seed_gate_failed_reason,
                    lifecycle_phase, elapsed_seconds, phase_seed_pair_threshold, phase_max_initial_ask_sum,
                    seed_window_open, normal_add_window_open, repair_only,
                    mode, allowed, proposed_qty, final_qty,
                    current_imbalance_ratio, projected_imbalance_ratio, current_settlement_EV,
                    projected_settlement_EV, current_worst_case_pnl, projected_worst_case_pnl, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["event_key"], event["market_ticker"], event["ts"], event["side"], event["event_type"],
                    event["reason"], event["price"], event["qty"], event["projected_paired_cost"], event["current_paired_cost"],
                    event["edge"], event["up_qty"], event["down_qty"], event["time_to_expiry"], event["slope"], event["atr"],
                    event["distance_from_strike"], event["up_bid"], event["up_ask"], event["down_bid"], event["down_ask"],
                    event.get("simple_ask_sum"), event.get("require_seed_pair_cost_below"), event.get("max_initial_ask_sum"), event.get("seed_gate_failed_reason"),
                    str(event.get("lifecycle_phase")) if event.get("lifecycle_phase") is not None else None, event.get("elapsed_seconds"), event.get("phase_seed_pair_threshold"), event.get("phase_max_initial_ask_sum"),
                    int(bool(event.get("seed_window_open"))) if event.get("seed_window_open") is not None else None,
                    int(bool(event.get("normal_add_window_open"))) if event.get("normal_add_window_open") is not None else None,
                    int(bool(event.get("repair_only"))) if event.get("repair_only") is not None else None,
                    event.get("mode"), event.get("allowed"), event.get("proposed_qty"), event.get("final_qty"),
                    event.get("current_imbalance_ratio"), event.get("projected_imbalance_ratio"), event.get("current_settlement_EV"),
                    event.get("projected_settlement_EV"), event.get("current_worst_case_pnl"), event.get("projected_worst_case_pnl"),
                    json.dumps(event["raw_json"], sort_keys=True),
                ),
            )
        self.manager.events.clear()

    def _cumulative_metrics(self) -> dict[str, Any]:
        if not self.results_db.exists():
            return _empty_cumulative_metrics()
        with sqlite3.connect(f"file:{self.results_db}?mode=ro", uri=True, timeout=2.0) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            snapshots = _table_count(conn, "processed_snapshots")
            events = _table_count(conn, "volatility_hedge_events")
            fills = _event_count(conn, "fill")
            notional = _sum_event_notional(conn)
            settled = _settled_position_metrics(conn)
        institutional = dict(settled)
        institutional["notional"] = notional
        institutional["trades"] = fills
        return {
            "snapshots": snapshots,
            "signals": snapshots,
            "fills": fills,
            "notional": notional,
            "volatility_hedge_events": events,
            "settled_positions": institutional.get("settled_trades", 0),
            "institutional_metrics": institutional,
            **institutional,
        }

    def summary(self) -> dict[str, Any]:
        positions = list(self.manager.positions.values())
        return {
            "strategy": "volatility_hedge",
            "volatility_hedge_positions": len(positions),
            "up_qty": round(sum(position.up_qty for position in positions), 10),
            "down_qty": round(sum(position.down_qty for position in positions), 10),
            "paired_qty": round(sum(position.paired_qty for position in positions), 10),
            "avg_paired_cost": _avg([position.paired_cost for position in positions if position.paired_cost is not None]),
            "avg_edge": _avg([position.edge for position in positions if position.edge is not None]),
            "locked_edge_dollars": round(sum(position.locked_edge_dollars for position in positions), 10),
            "total_cost": round(sum(position.total_cost for position in positions), 10),
            "max_imbalance_ratio": round(max((position.imbalance_ratio for position in positions), default=1.0), 10),
        }


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _slope_from_state(state: MarketState) -> float | None:
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


def _empty_cumulative_metrics() -> dict[str, Any]:
    institutional = _metrics_from_pnls([])
    institutional.update({"notional": 0.0, "trades": 0, "settled_trades": 0, "settlement_source": None})
    return {
        "snapshots": 0,
        "signals": 0,
        "fills": 0,
        "notional": 0.0,
        "volatility_hedge_events": 0,
        "settled_positions": 0,
        "institutional_metrics": institutional,
        **institutional,
    }


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _event_count(conn: sqlite3.Connection, event_type: str) -> int:
    if not _table_exists(conn, "volatility_hedge_events"):
        return 0
    return int(conn.execute("SELECT COUNT(*) FROM volatility_hedge_events WHERE event_type = ?", (event_type,)).fetchone()[0])


def _sum_event_notional(conn: sqlite3.Connection) -> float:
    if not _table_exists(conn, "volatility_hedge_events"):
        return 0.0
    row = conn.execute("SELECT COALESCE(SUM(COALESCE(price, 0) * qty), 0) FROM volatility_hedge_events WHERE event_type = 'fill'").fetchone()
    return round(float(row[0] or 0.0), 10)


def _settled_position_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(conn, "volatility_hedge_positions"):
        metrics = _metrics_from_pnls([])
        metrics.update({"settled_trades": 0, "settlement_source": None})
        return metrics
    rows = conn.execute(
        """
        SELECT market_ticker, strike, up_qty, down_qty, total_cost, market_close_time
        FROM volatility_hedge_positions
        WHERE market_close_time <= datetime('now')
        """
    ).fetchall()
    pnls: list[float] = []
    sources: set[str] = set()
    for row in rows:
        settlement = _settlement_for_market(conn, str(row["market_ticker"]), str(row["market_close_time"]))
        if settlement is None:
            continue
        winner, source = settlement
        sources.add(source)
        payout = float(row["up_qty"] if winner == "UP" else row["down_qty"])
        pnls.append(payout - float(row["total_cost"] or 0.0))
    metrics = _metrics_from_pnls(pnls)
    metrics["settled_trades"] = len(pnls)
    metrics["settlement_source"] = ",".join(sorted(sources)) if sources else None
    return metrics


def _settlement_for_market(conn: sqlite3.Connection, market_ticker: str, close_time: str) -> tuple[str, str] | None:
    if _table_exists(conn, "market_settlements"):
        row = conn.execute("SELECT settlement_result, settlement_source FROM market_settlements WHERE market_ticker = ?", (market_ticker,)).fetchone()
        if row and row["settlement_result"]:
            result = str(row["settlement_result"]).lower()
            if result in {"above", "yes", "up"}:
                return "UP", str(row["settlement_source"] or "kalshi_official")
            if result in {"below", "no", "down"}:
                return "DOWN", str(row["settlement_source"] or "kalshi_official")
    if not _table_exists(conn, "realtime_snapshots_1s"):
        return None
    row = conn.execute(
        """
        SELECT btc_price, strike
        FROM realtime_snapshots_1s
        WHERE market_ticker = ? AND ts <= ?
        ORDER BY ts DESC
        LIMIT 1
        """,
        (market_ticker, close_time),
    ).fetchone()
    if row is None:
        return None
    return ("UP" if float(row["btc_price"]) > float(row["strike"]) else "DOWN", "live_final_snapshot")


def _metrics_from_pnls(pnls: list[float]) -> dict[str, Any]:
    trades = len(pnls)
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    total = sum(pnls)
    equity = peak = max_drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    returns = pnls
    std = pstdev(returns) if len(returns) > 1 else 0.0
    sharpe = (mean(returns) / std) if std else 0.0
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    return {
        "win_rate": len(wins) / trades if trades else 0.0,
        "ev_per_trade": total / trades if trades else 0.0,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "profit_factor": abs(gross_profit / gross_loss) if gross_loss < 0 else (float("inf") if gross_profit > 0 else 0.0),
        "total_pnl": total,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None
