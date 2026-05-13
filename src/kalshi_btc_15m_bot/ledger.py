from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import PaperConfig
from .models import KalshiMarket, Prediction, now_utc


@dataclass(frozen=True)
class PaperAccount:
    initial_cash: float
    cash: float
    open_notional: float
    realized_pnl: float
    open_trades: int
    settled_trades: int


class PaperLedger:
    def __init__(self, path: Path, paper_config: PaperConfig) -> None:
        self.path = path
        self.paper_config = paper_config
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS predictions (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    market_ticker TEXT NOT NULL,
                    event_ticker TEXT NOT NULL,
                    market_close_time TEXT,
                    action TEXT NOT NULL,
                    side TEXT,
                    probability_yes REAL NOT NULL,
                    probability_no REAL NOT NULL,
                    confidence REAL NOT NULL,
                    edge REAL NOT NULL,
                    stake_dollars REAL NOT NULL,
                    current_price REAL NOT NULL,
                    target_price REAL,
                    yes_ask REAL NOT NULL,
                    no_ask REAL NOT NULL,
                    model_info_json TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    features_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS paper_trades (
                    id TEXT PRIMARY KEY,
                    prediction_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    market_ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    contracts REAL NOT NULL,
                    notional REAL NOT NULL,
                    status TEXT NOT NULL,
                    market_close_time TEXT,
                    settlement_result TEXT,
                    realized_pnl REAL,
                    settled_at TEXT,
                    FOREIGN KEY(prediction_id) REFERENCES predictions(id)
                );

                CREATE INDEX IF NOT EXISTS idx_predictions_created_at ON predictions(created_at);
                CREATE INDEX IF NOT EXISTS idx_trades_market_status ON paper_trades(market_ticker, status);
                """
            )

    def record_prediction(self, prediction: Prediction) -> None:
        data = prediction.to_jsonable()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO predictions (
                    id, created_at, market_ticker, event_ticker, market_close_time,
                    action, side, probability_yes, probability_no, confidence, edge,
                    stake_dollars, current_price, target_price, yes_ask, no_ask,
                    model_info_json, reasons_json, features_json, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prediction.prediction_id,
                    prediction.created_at.isoformat(),
                    prediction.market.ticker,
                    prediction.market.event_ticker,
                    prediction.market.close_time.isoformat() if prediction.market.close_time else None,
                    prediction.action,
                    prediction.side,
                    prediction.probability_yes,
                    prediction.probability_no,
                    prediction.confidence,
                    prediction.edge,
                    prediction.stake_dollars,
                    prediction.current_price,
                    prediction.market.target_price,
                    prediction.market.yes_ask,
                    prediction.market.no_ask,
                    _json(data["model_info"]),
                    _json(prediction.reasons),
                    _json(prediction.feature_snapshot),
                    _json(data),
                ),
            )

    def maybe_open_paper_trade(self, prediction: Prediction) -> str | None:
        if not self.paper_config.auto_trade or not prediction.action.startswith("BUY_"):
            return None
        if prediction.side not in {"YES", "NO"}:
            return None
        if self.paper_config.one_trade_per_market and self.has_open_or_settled_trade(prediction.market.ticker):
            return None

        account = self.account()
        stake = min(prediction.stake_dollars, self.paper_config.max_position_dollars, account.cash)
        if stake < 1.0:
            return None
        entry_price = prediction.market.yes_ask if prediction.side == "YES" else prediction.market.no_ask
        if not (0.0 < entry_price < 1.0):
            return None

        trade_id = f"paper-{prediction.prediction_id[:12]}"
        contracts = stake / entry_price
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO paper_trades (
                    id, prediction_id, created_at, market_ticker, side, entry_price,
                    contracts, notional, status, market_close_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
                """,
                (
                    trade_id,
                    prediction.prediction_id,
                    prediction.created_at.isoformat(),
                    prediction.market.ticker,
                    prediction.side,
                    entry_price,
                    contracts,
                    stake,
                    prediction.market.close_time.isoformat() if prediction.market.close_time else None,
                ),
            )
        return trade_id

    def has_open_or_settled_trade(self, market_ticker: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM paper_trades WHERE market_ticker = ?", (market_ticker,)
            ).fetchone()
        return bool(row and row["n"])

    def open_trades(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM paper_trades WHERE status = 'OPEN' ORDER BY created_at DESC"
                ).fetchall()
            )

    def settle_market(self, market: KalshiMarket) -> int:
        result = str(market.raw.get("result") or "").lower()
        if result not in {"yes", "no"}:
            return 0
        settled = 0
        settled_at = now_utc().isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE market_ticker = ? AND status = 'OPEN'",
                (market.ticker,),
            ).fetchall()
            for row in rows:
                won = (row["side"] == "YES" and result == "yes") or (
                    row["side"] == "NO" and result == "no"
                )
                payout = float(row["contracts"]) if won else 0.0
                pnl = payout - float(row["notional"])
                conn.execute(
                    """
                    UPDATE paper_trades
                    SET status = 'SETTLED', settlement_result = ?, realized_pnl = ?, settled_at = ?
                    WHERE id = ?
                    """,
                    (result, pnl, settled_at, row["id"]),
                )
                settled += 1
        return settled

    def account(self) -> PaperAccount:
        with self.connect() as conn:
            open_row = conn.execute(
                "SELECT COALESCE(SUM(notional), 0) AS open_notional, COUNT(*) AS n "
                "FROM paper_trades WHERE status = 'OPEN'"
            ).fetchone()
            settled_row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) AS pnl, COUNT(*) AS n "
                "FROM paper_trades WHERE status = 'SETTLED'"
            ).fetchone()
        open_notional = float(open_row["open_notional"])
        realized_pnl = float(settled_row["pnl"])
        cash = self.paper_config.initial_cash + realized_pnl - open_notional
        return PaperAccount(
            initial_cash=self.paper_config.initial_cash,
            cash=cash,
            open_notional=open_notional,
            realized_pnl=realized_pnl,
            open_trades=int(open_row["n"]),
            settled_trades=int(settled_row["n"]),
        )

    def latest_predictions(self, limit: int = 10) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM predictions ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            )

    def latest_trades(self, limit: int = 10) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM paper_trades ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            )


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _json(value: Any) -> str:
    return json.dumps(_jsonable(value), sort_keys=True)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
        return None
    return value
