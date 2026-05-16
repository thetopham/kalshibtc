from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
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
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
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
                    exit_price REAL,
                    exit_reason TEXT,
                    FOREIGN KEY(prediction_id) REFERENCES predictions(id)
                );

                CREATE INDEX IF NOT EXISTS idx_predictions_created_at ON predictions(created_at);
                CREATE INDEX IF NOT EXISTS idx_trades_market_status ON paper_trades(market_ticker, status);
                """
            )
            _ensure_column(conn, "paper_trades", "exit_price", "REAL")
            _ensure_column(conn, "paper_trades", "exit_reason", "TEXT")

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

    def paper_entry_skip_reason(self, prediction: Prediction) -> str | None:
        if not self.paper_config.auto_trade:
            return "auto_trade_disabled"
        if not prediction.action.startswith("BUY_"):
            return "action_not_buy"
        if prediction.side not in {"YES", "NO"}:
            return "invalid_side"
        if self.paper_config.one_trade_per_market and self.has_open_or_settled_trade(prediction.market.ticker):
            return "one_trade_per_market: already traded this market"

        account = self.account()
        if account.open_trades >= self.paper_config.max_open_trades:
            return f"max_open_trades: {account.open_trades} >= {self.paper_config.max_open_trades}"
        if self._daily_trade_count(prediction.created_at) >= self.paper_config.max_daily_trades:
            return f"max_daily_trades: reached {self.paper_config.max_daily_trades}"
        daily_pnl = self._daily_realized_pnl(prediction.created_at)
        if daily_pnl <= -abs(self.paper_config.max_daily_loss_dollars):
            return f"max_daily_loss: realized_pnl {daily_pnl:.2f}"
        if prediction.market.liquidity < self.paper_config.min_liquidity_dollars:
            return (
                "min_liquidity: "
                f"{prediction.market.liquidity:.2f} below {self.paper_config.min_liquidity_dollars:.2f}"
            )

        entry_price = prediction.market.yes_ask if prediction.side == "YES" else prediction.market.no_ask
        exit_bid = prediction.market.yes_bid if prediction.side == "YES" else prediction.market.no_bid
        if not (0.0 < entry_price < 1.0):
            return "invalid_entry_price"
        spread = entry_price - exit_bid
        if spread > self.paper_config.max_spread:
            return f"max_spread: side spread {spread:.3f} exceeds {self.paper_config.max_spread:.3f}"

        stake = min(prediction.stake_dollars, self.paper_config.max_position_dollars, account.cash)
        if stake < 1.0:
            return "stake_below_minimum"
        return None

    def maybe_open_paper_trade(self, prediction: Prediction) -> str | None:
        if self.paper_entry_skip_reason(prediction) is not None:
            return None

        account = self.account()
        entry_price = prediction.market.yes_ask if prediction.side == "YES" else prediction.market.no_ask
        stake = min(prediction.stake_dollars, self.paper_config.max_position_dollars, account.cash)
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

    def _daily_trade_count(self, at: datetime) -> int:
        day_prefix = at.date().isoformat()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM paper_trades WHERE substr(created_at, 1, 10) = ?",
                (day_prefix,),
            ).fetchone()
        return int(row["n"] if row else 0)

    def _daily_realized_pnl(self, at: datetime) -> float:
        day_prefix = at.date().isoformat()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(realized_pnl), 0) AS pnl
                FROM paper_trades
                WHERE status IN ('SETTLED', 'CLOSED') AND substr(COALESCE(settled_at, created_at), 1, 10) = ?
                """,
                (day_prefix,),
            ).fetchone()
        return float(row["pnl"] if row else 0.0)

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
                exit_price = 1.0 if won else 0.0
                conn.execute(
                    """
                    UPDATE paper_trades
                    SET status = 'SETTLED', settlement_result = ?, realized_pnl = ?, settled_at = ?,
                        exit_price = ?, exit_reason = ?
                    WHERE id = ?
                    """,
                    (result, pnl, settled_at, exit_price, f"settlement_{result}", row["id"]),
                )
                settled += 1
        return settled

    def close_paper_trade(
        self,
        trade_id: str,
        *,
        exit_price: float,
        exit_reason: str,
        closed_at: datetime | None = None,
    ) -> int:
        if not (0.0 <= exit_price <= 1.0):
            raise ValueError("exit_price must be between 0 and 1")
        closed_at_iso = (closed_at or now_utc()).isoformat()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM paper_trades WHERE id = ? AND status = 'OPEN'",
                (trade_id,),
            ).fetchone()
            if row is None:
                return 0
            realized_pnl = float(row["contracts"]) * exit_price - float(row["notional"])
            conn.execute(
                """
                UPDATE paper_trades
                SET status = 'CLOSED', settlement_result = ?, realized_pnl = ?, settled_at = ?,
                    exit_price = ?, exit_reason = ?
                WHERE id = ? AND status = 'OPEN'
                """,
                (exit_reason, realized_pnl, closed_at_iso, exit_price, exit_reason, trade_id),
            )
        return 1

    def account(self) -> PaperAccount:
        with self.connect() as conn:
            open_row = conn.execute(
                "SELECT COALESCE(SUM(notional), 0) AS open_notional, COUNT(*) AS n "
                "FROM paper_trades WHERE status = 'OPEN'"
            ).fetchone()
            settled_row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) AS pnl, COUNT(*) AS n "
                "FROM paper_trades WHERE status IN ('SETTLED', 'CLOSED')"
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

    def performance_summary(self, open_marks: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
        open_marks = open_marks or []
        account = self.account()
        with self.connect() as conn:
            prediction_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total_predictions,
                    COALESCE(SUM(CASE WHEN action LIKE 'BUY_%' THEN 1 ELSE 0 END), 0) AS buy_signals
                FROM predictions
                """
            ).fetchone()
            trade_row = conn.execute("SELECT COUNT(*) AS total_trades FROM paper_trades").fetchone()
            closed_rows = conn.execute(
                """
                SELECT notional, realized_pnl
                FROM paper_trades
                WHERE status IN ('SETTLED', 'CLOSED') AND realized_pnl IS NOT NULL
                ORDER BY settled_at ASC
                """
            ).fetchall()

        pnls = [float(row["realized_pnl"]) for row in closed_rows]
        closed_notional = sum(float(row["notional"]) for row in closed_rows)
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        closed_count = len(pnls)
        open_unrealized = sum(float(mark.get("unrealized_pnl", 0.0)) for mark in open_marks if not mark.get("quote_error"))
        open_current_value = sum(float(mark.get("current_value", 0.0)) for mark in open_marks if not mark.get("quote_error"))
        marked_open_notional = sum(float(mark.get("notional", 0.0)) for mark in open_marks if not mark.get("quote_error"))
        fallback_open_value = account.open_notional - marked_open_notional
        total_equity = account.cash + open_current_value + fallback_open_value

        return {
            "total_predictions": int(prediction_row["total_predictions"] if prediction_row else 0),
            "buy_signals": int(prediction_row["buy_signals"] if prediction_row else 0),
            "total_trades": int(trade_row["total_trades"] if trade_row else 0),
            "open_trades": account.open_trades,
            "closed_trades": closed_count,
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": (len(wins) / closed_count) if closed_count else 0.0,
            "realized_pnl": account.realized_pnl,
            "open_unrealized_pnl": open_unrealized,
            "total_equity": total_equity,
            "expectancy_dollars": (sum(pnls) / closed_count) if closed_count else 0.0,
            "realized_roi_on_risk": (account.realized_pnl / closed_notional) if closed_notional else 0.0,
            "largest_win": max(wins) if wins else 0.0,
            "largest_loss": min(losses) if losses else 0.0,
            "closed_notional": closed_notional,
        }

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


def mark_open_trade_to_market(row: Mapping[str, Any], market: KalshiMarket) -> dict[str, Any]:
    """Return a conservative mark-to-market snapshot for an open paper trade.

    The mark uses the side's bid first because that is the public exit price a
    paper trader could plausibly sell into. If the bid is unavailable, it falls
    back to the side mid so the status report remains useful but labels the
    source clearly.
    """
    side = str(row["side"])
    if side == "YES":
        mark_price = market.yes_bid
        price_source = "yes_bid"
        fallback_price = market.yes_mid
    elif side == "NO":
        mark_price = market.no_bid
        price_source = "no_bid"
        fallback_price = market.no_mid
    else:
        raise ValueError(f"Unsupported paper trade side: {side}")

    if mark_price <= 0.0:
        mark_price = fallback_price
        price_source = f"{side.lower()}_mid_fallback"

    entry_price = float(row["entry_price"])
    contracts = float(row["contracts"])
    notional = float(row["notional"])
    current_value = contracts * mark_price
    unrealized_pnl = current_value - notional
    unrealized_pnl_pct = unrealized_pnl / notional if notional else 0.0
    max_payout = contracts
    max_profit_if_correct = max_payout - notional

    return {
        "trade_id": row["id"],
        "prediction_id": row["prediction_id"],
        "market_ticker": row["market_ticker"],
        "side": side,
        "entry_price": entry_price,
        "contracts": contracts,
        "notional": notional,
        "mark_price": mark_price,
        "price_source": price_source,
        "current_value": current_value,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pnl_pct": unrealized_pnl_pct,
        "max_payout": max_payout,
        "max_profit_if_correct": max_profit_if_correct,
        "market_close_time": market.close_time.isoformat() if market.close_time else row["market_close_time"],
        "market_status": market.status,
        "liquidity": market.liquidity,
        "yes_bid": market.yes_bid,
        "yes_ask": market.yes_ask,
        "no_bid": market.no_bid,
        "no_ask": market.no_ask,
    }


def evaluate_paper_exit(
    row: Mapping[str, Any],
    mark: Mapping[str, Any],
    paper_config: PaperConfig,
    *,
    now: datetime | None = None,
) -> str | None:
    if not paper_config.manage_positions or mark.get("quote_error"):
        return None
    now = now or now_utc()
    pnl_pct = float(mark.get("unrealized_pnl_pct", 0.0))
    if pnl_pct >= paper_config.take_profit_pct:
        return "take_profit"
    if pnl_pct <= paper_config.stop_loss_pct:
        return "stop_loss"

    close_time_value = mark.get("market_close_time") or _mapping_get(row, "market_close_time")
    close_time = parse_iso(str(close_time_value or ""))
    if close_time is not None:
        seconds_to_close = (close_time - now).total_seconds()
        if seconds_to_close <= paper_config.force_close_seconds_to_close:
            return "time_exit"
    return None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _mapping_get(row: Mapping[str, Any], key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
