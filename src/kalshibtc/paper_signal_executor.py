from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_btc_15m_bot.kalshi_client import KalshiPublicClient

from .config import RiskLimits
from .datafeed.models import OrderBookSnapshot, Tick
from .execution.risk import RiskDecision, RiskManager
from .main import MarketStateBuilder
from .market.contract import ContractWindow
from .market.state import MarketState
from .strategy.signals import Signal, Strategy
from .strategy.simple_directional import SimpleDirectionalStrategy

STREAM_TABLE = "realtime_snapshots_1s"
KALSHI_PUBLIC_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
SETTLEMENT_SOURCE_OFFICIAL = "kalshi_official"
SETTLEMENT_SOURCE_ESTIMATE = "coinbase_estimate"


@dataclass(frozen=True)
class SettlementDecision:
    outcome: str
    settled_at: str
    source: str
    exit_reason_prefix: str
    official_result: str | None = None
    official_expiration_value: float | None = None
    settlement_value_dollars: float | None = None
    raw_json: str | None = None


@dataclass(frozen=True)
class PaperRunSummary:
    snapshots_processed: int = 0
    signals_recorded: int = 0
    trades_opened: int = 0
    trades_closed: int = 0
    skipped_snapshots: int = 0


class OneSecondPaperTrader:
    """Paper-only executor for the 1s websocket tape.

    The source snapshot database is recorder-owned. This class only reads rows
    from it and writes all generated signals, fake fills, exits, offsets, and
    PnL into a separate results database.
    """

    def __init__(
        self,
        *,
        snapshot_db: str | Path,
        ledger_db: str | Path,
        strategies: Sequence[Strategy] | None = None,
        risk_limits: RiskLimits | None = None,
        official_settlement_client: Any | None = None,
    ) -> None:
        self.snapshot_db = Path(snapshot_db)
        self.ledger_db = Path(ledger_db)
        if self.snapshot_db.resolve() == self.ledger_db.resolve():
            raise ValueError("snapshot_db and ledger_db must be separate databases")
        self.strategies = list(strategies or [SimpleDirectionalStrategy()])
        self.risk_manager = RiskManager(risk_limits or RiskLimits())
        self.official_settlement_client = official_settlement_client
        initialize_results_db(self.ledger_db)

    def run_once(self, *, limit: int = 250) -> PaperRunSummary:
        processed = signals = opened = closed = skipped = 0
        official_cache: dict[str, SettlementDecision | None] = {}
        with (
            _connect_stream(self.snapshot_db) as stream,
            _connect_results(self.ledger_db) as results,
        ):
            rows = _select_unprocessed_snapshots(stream, results, limit=limit)
            for row in rows:
                snapshot_key = _snapshot_key(row)
                if _is_snapshot_processed(results, snapshot_key):
                    _advance_cursor(results, row)
                    skipped += 1
                    continue
                try:
                    state = _state_from_snapshot(row)
                except (KeyError, TypeError, ValueError):
                    _mark_snapshot_processed(results, snapshot_key)
                    _advance_cursor(results, row)
                    skipped += 1
                    continue

                processed += 1
                _upgrade_estimated_settlements(
                    results,
                    self.official_settlement_client,
                    official_cache=official_cache,
                )
                closed += _settle_expired_positions(
                    results,
                    state,
                    official_client=self.official_settlement_client,
                    official_cache=official_cache,
                )
                closed += _settle_expired_positions_from_stream(
                    results,
                    stream,
                    state.tick.ts,
                    official_client=self.official_settlement_client,
                    official_cache=official_cache,
                )
                open_positions = _open_position_count(results)
                for strategy in self.strategies:
                    signal = strategy.on_tick(state)
                    risk = self.risk_manager.evaluate(state, signal, open_positions=open_positions)
                    action, paper_side = _action_and_side(signal)
                    prediction_id = _prediction_id(state, signal.strategy)
                    _record_prediction(
                        results,
                        prediction_id=prediction_id,
                        state=state,
                        signal=signal,
                        risk=risk,
                        action=action,
                        paper_side=paper_side,
                    )
                    signals += 1
                    if _can_open_trade(state, risk, paper_side):
                        trade_opened = _open_trade(
                            results,
                            prediction_id=prediction_id,
                            state=state,
                            signal=signal,
                            risk=risk,
                            paper_side=paper_side,
                        )
                        if trade_opened:
                            opened += 1
                            open_positions += 1
                closed += _settle_expired_positions(
                    results,
                    state,
                    official_client=self.official_settlement_client,
                    official_cache=official_cache,
                )
                _mark_snapshot_processed(results, snapshot_key)
                _advance_cursor(results, row)
        return PaperRunSummary(
            snapshots_processed=processed,
            signals_recorded=signals,
            trades_opened=opened,
            trades_closed=closed,
            skipped_snapshots=skipped,
        )


def initialize_results_db(path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect_results(path) as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS executor_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS processed_snapshots (
                ts TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                PRIMARY KEY (ts, market_ticker)
            );

            CREATE TABLE IF NOT EXISTS predictions (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                event_ticker TEXT NOT NULL,
                market_close_time TEXT,
                strategy TEXT NOT NULL,
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
                strategy TEXT NOT NULL,
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
                settlement_source TEXT,
                official_result TEXT,
                official_expiration_value REAL,
                settlement_value_dollars REAL,
                settlement_raw_json TEXT,
                FOREIGN KEY(prediction_id) REFERENCES predictions(id)
            );

            CREATE INDEX IF NOT EXISTS idx_1s_predictions_created_at ON predictions(created_at);
            CREATE INDEX IF NOT EXISTS idx_1s_trades_market_status ON paper_trades(market_ticker, status);
            CREATE INDEX IF NOT EXISTS idx_1s_processed_snapshots_ts ON processed_snapshots(ts);
            """
        )
        _ensure_column(conn, "predictions", "strategy", "TEXT NOT NULL DEFAULT 'unknown'")
        _ensure_column(conn, "paper_trades", "strategy", "TEXT NOT NULL DEFAULT 'unknown'")
        _ensure_column(conn, "paper_trades", "settlement_source", "TEXT")
        _ensure_column(conn, "paper_trades", "official_result", "TEXT")
        _ensure_column(conn, "paper_trades", "official_expiration_value", "REAL")
        _ensure_column(conn, "paper_trades", "settlement_value_dollars", "REAL")
        _ensure_column(conn, "paper_trades", "settlement_raw_json", "TEXT")
        conn.execute(
            """
            UPDATE paper_trades
            SET settlement_source = ?
            WHERE status = 'SETTLED' AND settlement_source IS NULL
            """,
            (SETTLEMENT_SOURCE_ESTIMATE,),
        )


def count_paper_trades(path: str | Path) -> int:
    if not Path(path).exists():
        return 0
    with _connect_results(path) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM paper_trades").fetchone()
    return int(row["n"] if row else 0)


def _connect_stream(path: Path) -> sqlite3.Connection:
    # Intentionally not chmod/URI mode=ro/PRAGMA query_only: the recorder owns
    # this database and must keep writing the 1s stream. This connection simply
    # performs SELECTs and never mutates the stream schema or data. Refuse to
    # auto-create a typo'd stream path, though; only the recorder creates it.
    if not path.exists():
        raise FileNotFoundError(f"snapshot database not found: {path}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _connect_results(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _select_unprocessed_snapshots(
    stream: sqlite3.Connection,
    results: sqlite3.Connection,
    *,
    limit: int,
) -> list[sqlite3.Row]:
    if not _table_exists(stream, STREAM_TABLE):
        return []
    last_ts = _get_executor_state(results, "last_ts")
    last_market = _get_executor_state(results, "last_market_ticker") or ""
    capped_limit = max(1, int(limit))
    if last_ts:
        return list(
            stream.execute(
                f"""
                SELECT *
                FROM {STREAM_TABLE}
                WHERE ts > ? OR (ts = ? AND market_ticker > ?)
                ORDER BY ts ASC, market_ticker ASC
                LIMIT ?
                """,
                (last_ts, last_ts, last_market, capped_limit),
            ).fetchall()
        )
    return list(
        stream.execute(
            f"""
            SELECT *
            FROM {STREAM_TABLE}
            ORDER BY ts ASC, market_ticker ASC
            LIMIT ?
            """,
            (capped_limit,),
        ).fetchall()
    )


def _state_from_snapshot(row: sqlite3.Row) -> MarketState:
    ts = _parse_ts(str(row["ts"]))
    close_time = _parse_ts(str(row["market_close_time"]))
    strike = _float_value(_row_get(row, "target_price"), _float_value(row["strike"]))
    contract = ContractWindow(
        ticker=str(row["market_ticker"]),
        strike=strike,
        close_time=close_time,
        open_time=_parse_ts_or_none(_row_get(row, "market_open_time")),
    )
    tick = Tick(
        ts=ts,
        price=_float_value(_row_get(row, "btc_price", _row_get(row, "price"))),
        bid=None,
        ask=None,
        source="realtime_snapshots_1s",
        symbol="BTC-USD",
        raw=_row_json(row),
    )
    book = OrderBookSnapshot(
        ts=ts,
        market_ticker=contract.ticker,
        yes_bid=_float_or_none(_row_get(row, "yes_bid")),
        yes_ask=_float_or_none(_row_get(row, "yes_ask")),
        no_bid=_float_or_none(_row_get(row, "no_bid")),
        no_ask=_float_or_none(_row_get(row, "no_ask")),
        sequence=_int_or_none(_row_get(row, "orderbook_sequence")),
        raw=_row_json(row),
    )
    slope = _float_or_none(_row_get(row, "btc_velocity_30s", _row_get(row, "slope_30s")))
    return MarketStateBuilder(contract=contract).from_tick_and_book(
        tick=tick,
        orderbook=book,
        slope_30s=slope,
    )


def _record_prediction(
    conn: sqlite3.Connection,
    *,
    prediction_id: str,
    state: MarketState,
    signal: Signal,
    risk: RiskDecision,
    action: str,
    paper_side: str | None,
) -> None:
    features = _features(state)
    reasons = [signal.reason, *risk.blocked_by]
    probability_yes = _probability_yes_for_signal(signal)
    stake = risk.size_dollars if risk.allowed else 0.0
    edge = max(0.0, signal.confidence - 0.5) if risk.allowed else 0.0
    conn.execute(
        """
        INSERT OR REPLACE INTO predictions (
            id, created_at, market_ticker, event_ticker, market_close_time,
            strategy, action, side, probability_yes, probability_no, confidence,
            edge, stake_dollars, current_price, target_price, yes_ask, no_ask,
            model_info_json, reasons_json, features_json, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            prediction_id,
            state.tick.ts.isoformat(),
            state.contract.ticker,
            _event_ticker(state.contract.ticker),
            state.contract.close_time.isoformat(),
            signal.strategy,
            action,
            paper_side,
            probability_yes,
            1.0 - probability_yes,
            signal.confidence,
            edge,
            stake,
            state.price,
            state.strike,
            state.orderbook.yes_ask or 0.0,
            state.orderbook.no_ask or 0.0,
            _json({"name": "1s_simple_directional", "trained": False}),
            _json(reasons),
            _json(features),
            _json(
                {
                    "signal": {
                        "side": signal.side,
                        "reason": signal.reason,
                        "confidence": signal.confidence,
                        "strategy": signal.strategy,
                    },
                    "risk": {
                        "allowed": risk.allowed,
                        "side": risk.side,
                        "size_dollars": risk.size_dollars,
                        "entry_price": risk.entry_price,
                        "reason": risk.reason,
                        "blocked_by": risk.blocked_by,
                    },
                    "features": features,
                }
            ),
        ),
    )


def _open_trade(
    conn: sqlite3.Connection,
    *,
    prediction_id: str,
    state: MarketState,
    signal: Signal,
    risk: RiskDecision,
    paper_side: str | None,
) -> bool:
    if paper_side is None or risk.entry_price is None or risk.entry_price <= 0:
        return False
    trade_id = f"1s-paper-{prediction_id}"
    inserted = conn.execute(
        """
        INSERT OR IGNORE INTO paper_trades (
            id, prediction_id, created_at, market_ticker, strategy, side,
            entry_price, contracts, notional, status, market_close_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
        """,
        (
            trade_id,
            prediction_id,
            state.tick.ts.isoformat(),
            state.contract.ticker,
            signal.strategy,
            paper_side,
            risk.entry_price,
            risk.size_dollars / risk.entry_price,
            risk.size_dollars,
            state.contract.close_time.isoformat(),
        ),
    ).rowcount
    return bool(inserted)


def _settle_expired_positions(
    conn: sqlite3.Connection,
    state: MarketState,
    *,
    official_client: Any | None = None,
    official_cache: dict[str, SettlementDecision | None] | None = None,
) -> int:
    if state.tick.ts < state.contract.close_time and state.seconds_to_close > 0:
        return 0
    rows = conn.execute(
        "SELECT * FROM paper_trades WHERE market_ticker = ? AND status = 'OPEN'",
        (state.contract.ticker,),
    ).fetchall()
    if not rows:
        return 0
    settlement = _official_settlement_for_market(
        official_client,
        state.contract.ticker,
        official_cache=official_cache,
    ) or _estimated_settlement(
        price=state.price,
        strike=state.strike,
        settled_at=state.tick.ts.isoformat(),
    )
    return _settle_trade_rows(conn, rows, settlement=settlement)


def _settle_expired_positions_from_stream(
    conn: sqlite3.Connection,
    stream: sqlite3.Connection,
    now: datetime,
    *,
    official_client: Any | None = None,
    official_cache: dict[str, SettlementDecision | None] | None = None,
) -> int:
    rows = conn.execute(
        """
        SELECT *
        FROM paper_trades
        WHERE status = 'OPEN'
          AND market_close_time IS NOT NULL
          AND market_close_time <= ?
        """,
        (now.isoformat(),),
    ).fetchall()
    settled = 0
    for row in rows:
        market_ticker = str(row["market_ticker"])
        official = _official_settlement_for_market(
            official_client,
            market_ticker,
            official_cache=official_cache,
        )
        if official is not None:
            settled += _settle_trade_rows(conn, [row], settlement=official)
            continue

        settlement_row = _latest_settlement_snapshot(
            stream,
            market_ticker=market_ticker,
            close_time=str(row["market_close_time"]),
        )
        if settlement_row is None:
            continue
        try:
            price = _float_value(
                _row_get(settlement_row, "btc_price", _row_get(settlement_row, "price"))
            )
            strike = _float_value(
                _row_get(settlement_row, "target_price"),
                _float_value(settlement_row["strike"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        settlement = _estimated_settlement(price=price, strike=strike, settled_at=now.isoformat())
        settled += _settle_trade_rows(conn, [row], settlement=settlement)
    return settled


def _upgrade_estimated_settlements(
    conn: sqlite3.Connection,
    official_client: Any | None,
    *,
    official_cache: dict[str, SettlementDecision | None] | None = None,
) -> int:
    if official_client is None:
        return 0
    rows = conn.execute(
        """
        SELECT *
        FROM paper_trades
        WHERE status = 'SETTLED'
          AND COALESCE(settlement_source, ?) = ?
        """,
        (SETTLEMENT_SOURCE_ESTIMATE, SETTLEMENT_SOURCE_ESTIMATE),
    ).fetchall()
    upgraded = 0
    for row in rows:
        settlement = _official_settlement_for_market(
            official_client,
            str(row["market_ticker"]),
            official_cache=official_cache,
        )
        if settlement is None:
            continue
        cursor = conn.execute(
            """
            UPDATE paper_trades
            SET settlement_result = ?, realized_pnl = ?, settled_at = ?,
                exit_price = ?, exit_reason = ?, settlement_source = ?,
                official_result = ?, official_expiration_value = ?,
                settlement_value_dollars = ?, settlement_raw_json = ?
            WHERE id = ?
              AND status = 'SETTLED'
              AND COALESCE(settlement_source, ?) = ?
            """,
            _settlement_update_values(row, settlement)
            + (row["id"], SETTLEMENT_SOURCE_ESTIMATE, SETTLEMENT_SOURCE_ESTIMATE),
        )
        upgraded += cursor.rowcount
    return upgraded


def _latest_settlement_snapshot(
    stream: sqlite3.Connection,
    *,
    market_ticker: str,
    close_time: str,
) -> sqlite3.Row | None:
    row = stream.execute(
        f"""
        SELECT *
        FROM {STREAM_TABLE}
        WHERE market_ticker = ? AND ts <= ?
        ORDER BY ts DESC
        LIMIT 1
        """,
        (market_ticker, close_time),
    ).fetchone()
    if row is not None:
        return row
    return stream.execute(
        f"""
        SELECT *
        FROM {STREAM_TABLE}
        WHERE market_ticker = ? AND ts > ?
        ORDER BY ts ASC
        LIMIT 1
        """,
        (market_ticker, close_time),
    ).fetchone()


def _settlement_outcome(price: float, strike: float) -> str:
    return "above" if price > strike else "below" if price < strike else "at"


def _estimated_settlement(*, price: float, strike: float, settled_at: str) -> SettlementDecision:
    return SettlementDecision(
        outcome=_settlement_outcome(price, strike),
        settled_at=settled_at,
        source=SETTLEMENT_SOURCE_ESTIMATE,
        exit_reason_prefix="1s_expiry",
    )


def _official_settlement_for_market(
    official_client: Any | None,
    market_ticker: str,
    *,
    official_cache: dict[str, SettlementDecision | None] | None = None,
) -> SettlementDecision | None:
    if official_client is None:
        return None
    if official_cache is not None and market_ticker in official_cache:
        return official_cache[market_ticker]
    try:
        market = official_client.get_market(market_ticker)
    except Exception:
        settlement = None
    else:
        settlement = _official_settlement_from_market(market)
    if official_cache is not None:
        official_cache[market_ticker] = settlement
    return settlement


def _official_settlement_from_market(market: Any) -> SettlementDecision | None:
    raw = _official_market_mapping(market)
    result = _first_text(
        raw,
        "result",
        "settlement_result",
        "final_result",
        "winning_side",
        "outcome",
        "market_result",
    )
    outcome = _official_result_to_outcome(result)
    if outcome is None:
        return None
    official_result = _normalize_official_result(result, outcome)
    return SettlementDecision(
        outcome=outcome,
        settled_at=_first_text(raw, "settlement_ts", "settled_at", "expiration_time", "close_time")
        or datetime.now(tz=UTC).isoformat(),
        source=SETTLEMENT_SOURCE_OFFICIAL,
        exit_reason_prefix="kalshi_official",
        official_result=official_result,
        official_expiration_value=_first_float_from_mapping(
            raw,
            "expiration_value",
            "final_price",
            "underlying_price",
            "index_price",
        ),
        settlement_value_dollars=_first_float_from_mapping(
            raw,
            "settlement_value_dollars",
            "settlement_value",
            "payout_dollars",
        ),
        raw_json=_json(raw),
    )


def _official_market_mapping(market: Any) -> Mapping[str, Any]:
    if isinstance(market, Mapping):
        nested = market.get("market")
        return nested if isinstance(nested, Mapping) else market
    raw = getattr(market, "raw", None)
    if isinstance(raw, Mapping):
        nested = raw.get("market")
        return nested if isinstance(nested, Mapping) else raw
    to_jsonable = getattr(market, "to_jsonable", None)
    if callable(to_jsonable):
        data = to_jsonable()
        if isinstance(data, Mapping):
            return data
    return {}


def _official_result_to_outcome(result: str | None) -> str | None:
    if result is None:
        return None
    token = str(result).strip().lower().replace(" ", "_").replace("-", "_")
    if token in {"yes", "y", "true", "above", "yes_win", "yes_won", "yes_wins"}:
        return "above"
    if token in {
        "no",
        "n",
        "false",
        "below",
        "at_or_below",
        "no_win",
        "no_won",
        "no_wins",
    }:
        return "below"
    if token == "at":
        return "at"
    return None


def _normalize_official_result(result: str | None, outcome: str) -> str | None:
    if result is None:
        return None
    token = str(result).strip().lower()
    if token in {"yes", "no"}:
        return token
    if outcome == "above":
        return "yes"
    if outcome == "below":
        return "no"
    return token or None


def _first_text(mapping: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _first_float_from_mapping(mapping: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float_or_none(mapping.get(key))
        if value is not None:
            return value
    return None


def _settle_trade_rows(
    conn: sqlite3.Connection,
    rows: Sequence[sqlite3.Row],
    *,
    settlement: SettlementDecision,
) -> int:
    settled = 0
    for row in rows:
        cursor = conn.execute(
            """
            UPDATE paper_trades
            SET status = 'SETTLED', settlement_result = ?, realized_pnl = ?,
                settled_at = ?, exit_price = ?, exit_reason = ?, settlement_source = ?,
                official_result = ?, official_expiration_value = ?,
                settlement_value_dollars = ?, settlement_raw_json = ?
            WHERE id = ? AND status = 'OPEN'
            """,
            _settlement_update_values(row, settlement) + (row["id"],),
        )
        settled += cursor.rowcount
    return settled


def _settlement_update_values(row: sqlite3.Row, settlement: SettlementDecision) -> tuple[Any, ...]:
    side = str(row["side"]).upper()
    won = (side == "YES" and settlement.outcome == "above") or (
        side == "NO" and settlement.outcome == "below"
    )
    exit_price = 1.0 if won else 0.0
    pnl = float(row["contracts"]) * exit_price - float(row["notional"])
    return (
        settlement.outcome,
        pnl,
        settlement.settled_at,
        exit_price,
        f"{settlement.exit_reason_prefix}_{settlement.outcome}",
        settlement.source,
        settlement.official_result,
        settlement.official_expiration_value,
        settlement.settlement_value_dollars,
        settlement.raw_json,
    )


def _can_open_trade(state: MarketState, risk: RiskDecision, paper_side: str | None) -> bool:
    return bool(
        paper_side is not None
        and risk.allowed
        and risk.entry_price is not None
        and risk.entry_price > 0
        and state.seconds_to_close > 0
    )


def _open_position_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM paper_trades WHERE status = 'OPEN'").fetchone()
    return int(row["n"] if row else 0)


def _action_and_side(signal: Signal) -> tuple[str, str | None]:
    if signal.side == "long_above":
        return "BUY_YES", "YES"
    if signal.side == "long_below":
        return "BUY_NO", "NO"
    return "NO_TRADE", None


def _probability_yes_for_signal(signal: Signal) -> float:
    if signal.side == "long_above":
        return min(1.0, max(0.0, signal.confidence))
    if signal.side == "long_below":
        return min(1.0, max(0.0, 1.0 - signal.confidence))
    return 0.5


def _features(state: MarketState) -> dict[str, float]:
    return {
        "slope_at_entry": state.slope_30s or 0.0,
        "btc_velocity_30s": state.slope_30s or 0.0,
        "slope_30s": state.slope_30s or 0.0,
        "distance_from_strike": state.distance_from_strike,
        "seconds_to_expiry": state.seconds_to_close,
        "seconds_to_close": state.seconds_to_close,
    }


def _prediction_id(state: MarketState, strategy: str) -> str:
    safe_ts = state.tick.ts.isoformat().replace(":", "").replace("+", "Z")
    return f"1s-{strategy}-{state.contract.ticker}-{safe_ts}"


def _snapshot_key(row: sqlite3.Row) -> tuple[str, str]:
    return str(row["ts"]), str(row["market_ticker"])


def _is_snapshot_processed(conn: sqlite3.Connection, key: tuple[str, str]) -> bool:
    row = conn.execute(
        "SELECT 1 FROM processed_snapshots WHERE ts = ? AND market_ticker = ?",
        key,
    ).fetchone()
    return row is not None


def _mark_snapshot_processed(conn: sqlite3.Connection, key: tuple[str, str]) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO processed_snapshots (ts, market_ticker, processed_at)
        VALUES (?, ?, ?)
        """,
        (key[0], key[1], datetime.now(tz=UTC).isoformat()),
    )


def _advance_cursor(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    _set_executor_state(conn, "last_ts", str(row["ts"]))
    _set_executor_state(conn, "last_market_ticker", str(row["market_ticker"]))


def _get_executor_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM executor_state WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else None


def _set_executor_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO executor_state (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, datetime.now(tz=UTC).isoformat()),
    )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    return row[key] if key in row.keys() else default


def _row_json(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _float_value(value: Any, default: float | None = None) -> float:
    parsed = _float_or_none(value)
    if parsed is None:
        if default is None:
            raise ValueError("missing numeric value")
        return default
    return parsed


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_ts_or_none(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return _parse_ts(str(value))


def _event_ticker(market_ticker: str) -> str:
    parts = market_ticker.split("-")
    return parts[0] if parts else market_ticker


def _json(value: Any) -> str:
    return json.dumps(_jsonable(value), sort_keys=True)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m kalshibtc.paper_signal_executor",
        description="Paper-trade the 1s Kalshi BTC snapshot tape into a separate results database.",
    )
    parser.add_argument(
        "--snapshot-db",
        default="data/realtime-snapshots-1s.sqlite3",
        help="Recorder-owned 1s stream database to SELECT from; this command does not write to it.",
    )
    parser.add_argument(
        "--results-db",
        default="data-live-prod/paper-results-1s.sqlite3",
        help="Separate writable results database for signals, fake fills, exits, and PnL.",
    )
    parser.add_argument(
        "--limit", type=int, default=250, help="Max new snapshots to process per pass."
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Continuously poll the snapshot DB and write paper results until stopped.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=1.0,
        help="Sleep interval between loop passes when --loop is set.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON summaries.")
    parser.add_argument(
        "--kalshi-base-url",
        default=KALSHI_PUBLIC_BASE_URL,
        help="Kalshi public REST base URL used only to read official market results for settlement.",
    )
    parser.add_argument(
        "--official-settlement-timeout-seconds",
        type=int,
        default=10,
        help="Timeout for read-only Kalshi official settlement lookups.",
    )
    parser.add_argument(
        "--no-official-settlement",
        action="store_true",
        help="Disable Kalshi official result lookups and settle from Coinbase/raw 1s estimates only.",
    )
    args = parser.parse_args(argv)

    official_settlement_client = None
    if not args.no_official_settlement:
        official_settlement_client = KalshiPublicClient(
            base_url=args.kalshi_base_url,
            timeout_seconds=args.official_settlement_timeout_seconds,
        )

    trader = OneSecondPaperTrader(
        snapshot_db=Path(args.snapshot_db),
        ledger_db=Path(args.results_db),
        official_settlement_client=official_settlement_client,
    )

    def emit(summary: PaperRunSummary) -> None:
        data = summary.__dict__
        if args.json:
            print(json.dumps(data, sort_keys=True), flush=True)
        else:
            print(
                "1s_paper "
                f"processed={summary.snapshots_processed} "
                f"signals={summary.signals_recorded} "
                f"opened={summary.trades_opened} "
                f"closed={summary.trades_closed} "
                f"skipped={summary.skipped_snapshots}",
                flush=True,
            )

    while True:
        emit(trader.run_once(limit=args.limit))
        if not args.loop:
            return 0
        time.sleep(max(0.1, float(args.interval_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
