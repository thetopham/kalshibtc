from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..market.state import MarketState
from ..portfolio.hedge_position import HedgePosition
from ..strategy.hedge_volatility_v0 import HedgeDecision


class HedgePaperExecutor:
    """Paper-only SQLite recorder for hedge-volatility v0.

    This class writes only to the supplied results DB. It has no Kalshi client,
    no order submission method, and no network dependency.
    """

    def __init__(self, *, results_db: str | Path, strategy_name: str) -> None:
        self.results_db = Path(results_db)
        self.strategy_name = strategy_name
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> HedgePaperExecutor:
        self.results_db.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.results_db)
        _initialize_schema(self._conn)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._conn is None:
            return
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._conn.close()
        self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("HedgePaperExecutor is not open")
        return self._conn

    def apply(
        self,
        *,
        state: MarketState,
        position: HedgePosition,
        decision: HedgeDecision,
    ) -> None:
        position.add_fill(
            side=decision.side,
            price=decision.price,
            contracts=decision.contracts,
            ts=state.tick.ts,
            reason=decision.reason,
        )
        self._record_decision(
            state=state,
            decision="ALLOW",
            reason=decision.reason,
            position=position,
            side=decision.side,
            price=decision.price,
            contracts=decision.contracts,
            projected_combined_average_cost=decision.projected_combined_average_cost,
        )
        self.conn.execute(
            """
            INSERT INTO hedge_fills (
                ts, market_ticker, strategy, side, price, contracts, reason,
                projected_combined_average_cost, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.tick.ts.isoformat(),
                state.orderbook.market_ticker,
                self.strategy_name,
                decision.side,
                decision.price,
                decision.contracts,
                decision.reason,
                decision.projected_combined_average_cost,
                json.dumps(decision.__dict__, sort_keys=True),
            ),
        )
        self._upsert_position(state=state, position=position)

    def record_no_trade(self, *, state: MarketState, reason: str, position: HedgePosition) -> None:
        self._record_decision(
            state=state,
            decision="REJECT",
            reason=reason,
            position=position,
        )
        self._upsert_position(state=state, position=position)

    def _record_decision(
        self,
        *,
        state: MarketState,
        decision: str,
        reason: str,
        position: HedgePosition,
        side: str | None = None,
        price: float | None = None,
        contracts: float | None = None,
        projected_combined_average_cost: float | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO hedge_decisions (
                ts, market_ticker, strategy, decision, reason, side, price, contracts,
                projected_combined_average_cost, combined_average_cost, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.tick.ts.isoformat(),
                state.orderbook.market_ticker,
                self.strategy_name,
                decision,
                reason,
                side,
                price,
                contracts,
                projected_combined_average_cost,
                position.combined_average_cost,
                json.dumps(
                    {
                        "btc_price": state.price,
                        "strike": state.strike,
                        "distance_from_strike": state.distance_from_strike,
                        "seconds_to_close": state.seconds_to_close,
                        "slope_30s": state.slope_30s,
                        "yes_ask": state.orderbook.yes_ask,
                        "no_ask": state.orderbook.no_ask,
                    },
                    sort_keys=True,
                ),
            ),
        )

    def _upsert_position(self, *, state: MarketState, position: HedgePosition) -> None:
        self.conn.execute(
            """
            INSERT INTO hedge_positions (
                market_ticker, strategy, updated_at, yes_contracts, no_contracts,
                avg_yes_entry, avg_no_entry, combined_average_cost, locked_edge_per_pair,
                raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market_ticker, strategy) DO UPDATE SET
                updated_at=excluded.updated_at,
                yes_contracts=excluded.yes_contracts,
                no_contracts=excluded.no_contracts,
                avg_yes_entry=excluded.avg_yes_entry,
                avg_no_entry=excluded.avg_no_entry,
                combined_average_cost=excluded.combined_average_cost,
                locked_edge_per_pair=excluded.locked_edge_per_pair,
                raw_json=excluded.raw_json
            """,
            (
                position.market_ticker,
                self.strategy_name,
                state.tick.ts.isoformat(),
                position.yes_contracts,
                position.no_contracts,
                position.avg_yes_entry,
                position.avg_no_entry,
                position.combined_average_cost,
                position.locked_edge_per_pair,
                json.dumps([fill.__dict__ for fill in position.fills], default=str, sort_keys=True),
            ),
        )


def _initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS hedge_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            market_ticker TEXT NOT NULL,
            strategy TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL,
            side TEXT,
            price REAL,
            contracts REAL,
            projected_combined_average_cost REAL,
            combined_average_cost REAL,
            raw_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS hedge_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            market_ticker TEXT NOT NULL,
            strategy TEXT NOT NULL,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            contracts REAL NOT NULL,
            reason TEXT NOT NULL,
            projected_combined_average_cost REAL NOT NULL,
            raw_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS hedge_positions (
            market_ticker TEXT NOT NULL,
            strategy TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            yes_contracts REAL NOT NULL,
            no_contracts REAL NOT NULL,
            avg_yes_entry REAL,
            avg_no_entry REAL,
            combined_average_cost REAL,
            locked_edge_per_pair REAL,
            raw_json TEXT NOT NULL,
            PRIMARY KEY (market_ticker, strategy)
        );

        CREATE INDEX IF NOT EXISTS idx_hedge_decisions_market_ts
            ON hedge_decisions(market_ticker, ts);
        CREATE INDEX IF NOT EXISTS idx_hedge_fills_market_ts
            ON hedge_fills(market_ticker, ts);
        """
    )
