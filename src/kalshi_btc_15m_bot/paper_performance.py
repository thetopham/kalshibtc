from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

CLOSED_STATUSES = {"CLOSED", "SETTLED"}
PAPER_TRADES_TABLE = "paper_trades"
PREDICTIONS_TABLE = "predictions"
REALTIME_SNAPSHOTS_TABLE = "realtime_snapshots_1s"
REVIEW_BUCKET_KEYS = (
    "strategy",
    "side",
    "seconds_to_expiry",
    "distance_from_strike",
    "slope_at_entry",
    "hold_seconds",
)


def collect_paper_trading_performance(
    ledger_path: Path | str,
    *,
    snapshot_path: Path | str | None = None,
    recent_limit: int = 25,
    group_limit: int = 12,
    cumulative_limit: int = 500,
) -> dict[str, Any]:
    """Read paper-trading PnL from SQLite without touching trading/execution paths."""

    ledger = Path(ledger_path)
    snapshots = Path(snapshot_path) if snapshot_path is not None else None
    payload = _empty_payload(ledger, snapshots)
    schema_gaps: list[str] = payload["schema_gaps"]

    if not ledger.exists():
        schema_gaps.append("ledger_missing")
        return payload

    try:
        with _connect_readonly(ledger) as conn:
            tables = _table_names(conn)
            if PAPER_TRADES_TABLE not in tables:
                schema_gaps.append("paper_trades_table_missing")
                return payload

            trade_columns = _columns(conn, PAPER_TRADES_TABLE)
            required = {
                "id",
                "created_at",
                "market_ticker",
                "side",
                "entry_price",
                "contracts",
                "notional",
                "status",
                "realized_pnl",
                "settled_at",
                "exit_price",
                "exit_reason",
            }
            missing = sorted(required - trade_columns)
            if missing:
                schema_gaps.append(f"paper_trades_missing_columns:{','.join(missing)}")
                return payload

            has_predictions = PREDICTIONS_TABLE in tables
            prediction_columns = _columns(conn, PREDICTIONS_TABLE) if has_predictions else set()
            if not has_predictions:
                schema_gaps.append("predictions_table_missing: grouping by trade side only")
            if "strategy" not in trade_columns and "strategy" not in prediction_columns:
                schema_gaps.append("strategy_column_missing: grouped by prediction action/side and market")
            if "paper_orders" not in tables and "paper_fills" not in tables:
                schema_gaps.append("paper_orders_fills_missing: paper_trades are position-level rows")

            open_positions = _open_positions(conn, has_predictions=has_predictions)
            open_positions, unrealized_state = _mark_open_positions(open_positions, snapshots, schema_gaps)
            marked_open_positions = {
                str(position.get("trade_id")): position
                for position in open_positions
                if position.get("trade_id") is not None
            }

            metrics = _summary_metrics(conn, unrealized_state)
            review_trades = _review_trades(
                conn,
                has_predictions=has_predictions,
                trade_columns=trade_columns,
                prediction_columns=prediction_columns,
                marked_open_positions=marked_open_positions,
                limit=recent_limit,
            )
            payload.update(
                {
                    "metrics": metrics,
                    "open_positions": open_positions,
                    "recent_trades": _recent_trades(
                        conn,
                        has_predictions=has_predictions,
                        trade_columns=trade_columns,
                        limit=recent_limit,
                    ),
                    "review_trades": review_trades,
                    "review_buckets": _review_buckets(review_trades, limit=group_limit),
                    "cumulative_pnl": _cumulative_pnl(conn, limit=cumulative_limit),
                    "by_signal": _group_by_signal(
                        conn,
                        has_predictions=has_predictions,
                        limit=group_limit,
                    ),
                    "by_market": _group_by_market(conn, limit=group_limit),
                }
            )
    except sqlite3.Error as exc:
        schema_gaps.append(f"sqlite_read_error:{exc}")
    except OSError as exc:
        schema_gaps.append(f"sqlite_path_error:{exc}")

    return payload


def _empty_payload(ledger_path: Path, snapshot_path: Path | None) -> dict[str, Any]:
    return {
        "ledger_path": str(ledger_path),
        "snapshot_path": str(snapshot_path) if snapshot_path is not None else None,
        "schema_gaps": [],
        "metrics": {
            "total_trades": 0,
            "total_simulated_positions": 0,
            "open_positions": 0,
            "closed_positions": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "marked_open_unrealized_pnl": 0.0,
            "total_pnl": 0.0,
            "win_rate": 0.0,
            "avg_win": None,
            "avg_loss": None,
            "largest_win": None,
            "largest_loss": None,
            "avg_trade_pnl": 0.0,
            "total_notional": 0.0,
            "closed_notional": 0.0,
            "marked_open_positions": 0,
            "unmarked_open_positions": 0,
            "unrealized_source": "no_open_positions",
        },
        "open_positions": [],
        "recent_trades": [],
        "review_trades": [],
        "review_buckets": _empty_review_buckets(),
        "cumulative_pnl": [],
        "by_signal": [],
        "by_market": [],
    }


def _empty_review_buckets() -> dict[str, list[dict[str, Any]]]:
    return {key: [] for key in REVIEW_BUCKET_KEYS}


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _summary_metrics(
    conn: sqlite3.Connection,
    unrealized_state: Mapping[str, Any],
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total_trades,
            COALESCE(SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END), 0) AS open_positions,
            COALESCE(SUM(CASE WHEN status IN ('SETTLED', 'CLOSED') THEN 1 ELSE 0 END), 0) AS closed_positions,
            COALESCE(SUM(CASE WHEN status IN ('SETTLED', 'CLOSED') AND realized_pnl IS NOT NULL THEN 1 ELSE 0 END), 0) AS closed_positions_with_pnl,
            COALESCE(SUM(CASE WHEN status IN ('SETTLED', 'CLOSED') AND realized_pnl > 0 THEN 1 ELSE 0 END), 0) AS winning_trades,
            COALESCE(SUM(CASE WHEN status IN ('SETTLED', 'CLOSED') AND realized_pnl < 0 THEN 1 ELSE 0 END), 0) AS losing_trades,
            COALESCE(SUM(CASE WHEN status IN ('SETTLED', 'CLOSED') THEN realized_pnl ELSE 0 END), 0) AS realized_pnl,
            AVG(CASE WHEN status IN ('SETTLED', 'CLOSED') AND realized_pnl > 0 THEN realized_pnl END) AS avg_win,
            AVG(CASE WHEN status IN ('SETTLED', 'CLOSED') AND realized_pnl < 0 THEN realized_pnl END) AS avg_loss,
            MAX(CASE WHEN status IN ('SETTLED', 'CLOSED') THEN realized_pnl END) AS largest_win,
            MIN(CASE WHEN status IN ('SETTLED', 'CLOSED') THEN realized_pnl END) AS largest_loss,
            AVG(CASE WHEN status IN ('SETTLED', 'CLOSED') THEN realized_pnl END) AS avg_trade_pnl,
            COALESCE(SUM(notional), 0) AS total_notional,
            COALESCE(SUM(CASE WHEN status IN ('SETTLED', 'CLOSED') THEN notional ELSE 0 END), 0) AS closed_notional
        FROM paper_trades
        """
    ).fetchone()
    if row is None:
        return _empty_payload(Path(""), None)["metrics"]

    total_trades = _int(row["total_trades"])
    closed_with_pnl = _int(row["closed_positions_with_pnl"])
    wins = _int(row["winning_trades"])
    realized_pnl = _float(row["realized_pnl"])
    unrealized_pnl = unrealized_state.get("unrealized_pnl")
    total_pnl = None if unrealized_pnl is None else realized_pnl + float(unrealized_pnl)

    return {
        "total_trades": total_trades,
        "total_simulated_positions": total_trades,
        "open_positions": _int(row["open_positions"]),
        "closed_positions": _int(row["closed_positions"]),
        "closed_positions_with_pnl": closed_with_pnl,
        "winning_trades": wins,
        "losing_trades": _int(row["losing_trades"]),
        "realized_pnl": realized_pnl,
        "unrealized_pnl": _float_or_none(unrealized_pnl),
        "marked_open_unrealized_pnl": _float(unrealized_state.get("marked_open_unrealized_pnl")),
        "total_pnl": _float_or_none(total_pnl),
        "win_rate": (wins / closed_with_pnl) if closed_with_pnl else 0.0,
        "avg_win": _float_or_none(row["avg_win"]),
        "avg_loss": _float_or_none(row["avg_loss"]),
        "largest_win": _float_or_none(row["largest_win"]),
        "largest_loss": _float_or_none(row["largest_loss"]),
        "avg_trade_pnl": _float(row["avg_trade_pnl"]),
        "total_notional": _float(row["total_notional"]),
        "closed_notional": _float(row["closed_notional"]),
        "marked_open_positions": _int(unrealized_state.get("marked_open_positions")),
        "unmarked_open_positions": _int(unrealized_state.get("unmarked_open_positions")),
        "unrealized_source": str(unrealized_state.get("unrealized_source") or "unknown"),
    }


def _recent_trades(
    conn: sqlite3.Connection,
    *,
    has_predictions: bool,
    trade_columns: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    join = "LEFT JOIN predictions p ON p.id = t.prediction_id" if has_predictions else ""
    signal_expr = "COALESCE(p.action, t.side)" if has_predictions else "t.side"
    edge_expr = "p.edge" if has_predictions else "NULL"
    confidence_expr = "p.confidence" if has_predictions else "NULL"
    settlement_source_expr = "t.settlement_source" if "settlement_source" in trade_columns else "NULL"
    official_result_expr = "t.official_result" if "official_result" in trade_columns else "NULL"
    official_expiration_value_expr = (
        "t.official_expiration_value" if "official_expiration_value" in trade_columns else "NULL"
    )
    settlement_value_dollars_expr = (
        "t.settlement_value_dollars" if "settlement_value_dollars" in trade_columns else "NULL"
    )
    rows = conn.execute(
        f"""
        SELECT
            t.id AS trade_id,
            t.prediction_id,
            t.created_at,
            t.settled_at,
            COALESCE(t.settled_at, t.created_at) AS event_at,
            t.market_ticker,
            {signal_expr} AS signal,
            t.side,
            t.entry_price,
            t.exit_price,
            t.exit_reason,
            {settlement_source_expr} AS settlement_source,
            {official_result_expr} AS official_result,
            {official_expiration_value_expr} AS official_expiration_value,
            {settlement_value_dollars_expr} AS settlement_value_dollars,
            t.contracts,
            t.notional,
            t.status,
            t.realized_pnl,
            {edge_expr} AS edge,
            {confidence_expr} AS confidence
        FROM paper_trades t
        {join}
        ORDER BY t.created_at DESC, t.id DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    return [_normalize_trade_row(row) for row in rows]


def _review_trades(
    conn: sqlite3.Connection,
    *,
    has_predictions: bool,
    trade_columns: set[str],
    prediction_columns: set[str],
    marked_open_positions: Mapping[str, Mapping[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    can_join_predictions = has_predictions and "prediction_id" in trade_columns
    join = "LEFT JOIN predictions p ON p.id = t.prediction_id" if can_join_predictions else ""
    strategy_expr = _review_strategy_expr(
        trade_columns,
        prediction_columns,
        can_join_predictions=can_join_predictions,
    )
    signal_expr = "p.action" if can_join_predictions and "action" in prediction_columns else "t.side"
    features_expr = (
        "p.features_json" if can_join_predictions and "features_json" in prediction_columns else "NULL"
    )
    reasons_expr = (
        "p.reasons_json" if can_join_predictions and "reasons_json" in prediction_columns else "NULL"
    )
    current_price_expr = (
        "p.current_price" if can_join_predictions and "current_price" in prediction_columns else "NULL"
    )
    target_price_expr = (
        "p.target_price" if can_join_predictions and "target_price" in prediction_columns else "NULL"
    )
    close_exprs = []
    if "market_close_time" in trade_columns:
        close_exprs.append("t.market_close_time")
    if can_join_predictions and "market_close_time" in prediction_columns:
        close_exprs.append("p.market_close_time")
    close_time_expr = _coalesce_expr(close_exprs, fallback="NULL")
    prediction_id_expr = "t.prediction_id" if "prediction_id" in trade_columns else "NULL"
    settlement_source_expr = "t.settlement_source" if "settlement_source" in trade_columns else "NULL"
    official_result_expr = "t.official_result" if "official_result" in trade_columns else "NULL"
    official_expiration_value_expr = (
        "t.official_expiration_value" if "official_expiration_value" in trade_columns else "NULL"
    )
    settlement_value_dollars_expr = (
        "t.settlement_value_dollars" if "settlement_value_dollars" in trade_columns else "NULL"
    )
    rows = conn.execute(
        f"""
        SELECT
            t.id AS trade_id,
            {prediction_id_expr} AS prediction_id,
            t.created_at AS entry_time,
            t.settled_at AS exit_time,
            {close_time_expr} AS market_close_time,
            t.market_ticker,
            {strategy_expr} AS strategy,
            {signal_expr} AS signal,
            t.side,
            t.entry_price,
            t.exit_price,
            t.exit_reason,
            {settlement_source_expr} AS settlement_source,
            {official_result_expr} AS official_result,
            {official_expiration_value_expr} AS official_expiration_value,
            {settlement_value_dollars_expr} AS settlement_value_dollars,
            t.status,
            t.realized_pnl,
            {features_expr} AS features_json,
            {reasons_expr} AS reasons_json,
            {current_price_expr} AS current_price,
            {target_price_expr} AS target_price
        FROM paper_trades t
        {join}
        ORDER BY t.created_at DESC, t.id DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    return [_normalize_review_row(row, marked_open_positions) for row in rows]


def _review_strategy_expr(
    trade_columns: set[str],
    prediction_columns: set[str],
    *,
    can_join_predictions: bool,
) -> str:
    exprs = []
    if "strategy" in trade_columns:
        exprs.append("t.strategy")
    if can_join_predictions and "strategy" in prediction_columns:
        exprs.append("p.strategy")
    if can_join_predictions and "action" in prediction_columns:
        exprs.append("p.action")
    exprs.append("t.side")
    return _coalesce_expr(exprs, fallback="t.side")


def _coalesce_expr(exprs: list[str], *, fallback: str) -> str:
    if not exprs:
        return fallback
    if len(exprs) == 1:
        return exprs[0]
    return f"COALESCE({', '.join(exprs)})"


def _normalize_review_row(
    row: sqlite3.Row,
    marked_open_positions: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    data = dict(row)
    trade_id = str(data.get("trade_id") or "")
    mark = marked_open_positions.get(trade_id) or {}
    features = _json_mapping(data.get("features_json"))
    entry_time = data.get("entry_time")
    exit_time = data.get("exit_time")
    pnl = _float_or_none(data.get("realized_pnl"))
    if pnl is None:
        pnl = _float_or_none(mark.get("unrealized_pnl"))
    hold_seconds = _seconds_between(entry_time, exit_time or mark.get("mark_ts"))
    slope_at_entry = _first_float(
        features,
        "slope_at_entry",
        "btc_velocity_30s",
        "btc_slope_30s",
        "slope_30s",
        "price_slope",
    )
    distance_from_strike = _first_float(
        features,
        "distance_from_strike",
        "distance_to_strike",
        "distance_to_target",
        "target_distance",
    )
    if distance_from_strike is None:
        current_price = _float_or_none(data.get("current_price"))
        target_price = _float_or_none(data.get("target_price"))
        if current_price is not None and target_price is not None:
            distance_from_strike = current_price - target_price
    seconds_to_expiry = _first_float(
        features,
        "seconds_to_expiry",
        "seconds_to_close",
        "seconds_until_close",
        "time_to_expiry_seconds",
    )
    if seconds_to_expiry is None:
        seconds_to_expiry = _seconds_between(entry_time, data.get("market_close_time"))
    return {
        "trade_id": data.get("trade_id"),
        "prediction_id": data.get("prediction_id"),
        "market_ticker": data.get("market_ticker"),
        "strategy": data.get("strategy") or data.get("signal") or data.get("side") or "unknown",
        "side": data.get("side"),
        "status": data.get("status"),
        "entry_time": entry_time,
        "exit_time": exit_time,
        "entry_price": _float_or_none(data.get("entry_price")),
        "exit_price": _float_or_none(data.get("exit_price")),
        "settlement_source": data.get("settlement_source"),
        "settlement_source_label": _settlement_source_label(data.get("settlement_source")),
        "official_result": data.get("official_result"),
        "official_expiration_value": _float_or_none(data.get("official_expiration_value")),
        "settlement_value_dollars": _float_or_none(data.get("settlement_value_dollars")),
        "pnl": pnl,
        "hold_seconds": hold_seconds,
        "slope_at_entry": slope_at_entry,
        "distance_from_strike": distance_from_strike,
        "seconds_to_expiry": seconds_to_expiry,
        "reason": _review_reason(data.get("reasons_json"))
        or data.get("exit_reason")
        or data.get("signal")
        or data.get("strategy"),
    }


def _review_buckets(review_rows: list[dict[str, Any]], *, limit: int) -> dict[str, list[dict[str, Any]]]:
    capped_limit = max(1, int(limit))
    return {
        "strategy": _aggregate_review_buckets(
            review_rows,
            lambda row: (str(row.get("strategy") or "unknown"), 0),
            limit=capped_limit,
        ),
        "side": _aggregate_review_buckets(
            review_rows,
            lambda row: (str(row.get("side") or "unknown"), 0),
            limit=capped_limit,
        ),
        "seconds_to_expiry": _aggregate_review_buckets(
            review_rows,
            lambda row: _seconds_to_expiry_bucket(row.get("seconds_to_expiry")),
            limit=capped_limit,
        ),
        "distance_from_strike": _aggregate_review_buckets(
            review_rows,
            lambda row: _distance_from_strike_bucket(row.get("distance_from_strike")),
            limit=capped_limit,
        ),
        "slope_at_entry": _aggregate_review_buckets(
            review_rows,
            lambda row: _slope_at_entry_bucket(row.get("slope_at_entry")),
            limit=capped_limit,
        ),
        "hold_seconds": _aggregate_review_buckets(
            review_rows,
            lambda row: _hold_seconds_bucket(row.get("hold_seconds")),
            limit=capped_limit,
        ),
    }


def _aggregate_review_buckets(
    review_rows: list[dict[str, Any]],
    bucket_fn: Any,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for row in review_rows:
        bucket, bucket_order = bucket_fn(row)
        group = groups.setdefault(
            bucket,
            {
                "bucket": bucket,
                "bucket_order": bucket_order,
                "trades": 0,
                "pnl_trades": 0,
                "wins": 0,
                "total_pnl": 0.0,
                "hold_count": 0,
                "total_hold_seconds": 0.0,
            },
        )
        group["trades"] += 1
        pnl = _float_or_none(row.get("pnl"))
        if pnl is not None:
            group["pnl_trades"] += 1
            group["total_pnl"] += pnl
            if pnl > 0:
                group["wins"] += 1
        hold_seconds = _float_or_none(row.get("hold_seconds"))
        if hold_seconds is not None:
            group["hold_count"] += 1
            group["total_hold_seconds"] += hold_seconds

    normalized: list[dict[str, Any]] = []
    for group in groups.values():
        pnl_trades = _int(group.get("pnl_trades"))
        hold_count = _int(group.get("hold_count"))
        total_pnl = _float(group.get("total_pnl"))
        normalized.append(
            {
                "bucket": group.get("bucket"),
                "bucket_order": group.get("bucket_order"),
                "trades": _int(group.get("trades")),
                "pnl_trades": pnl_trades,
                "wins": _int(group.get("wins")),
                "win_rate": (_int(group.get("wins")) / pnl_trades) if pnl_trades else 0.0,
                "avg_pnl": (total_pnl / pnl_trades) if pnl_trades else None,
                "total_pnl": total_pnl,
                "avg_hold_seconds": (
                    _float(group.get("total_hold_seconds")) / hold_count if hold_count else None
                ),
            }
        )
    normalized.sort(key=lambda item: (_int(item.get("bucket_order")), str(item.get("bucket"))))
    return normalized[:limit]


def _seconds_to_expiry_bucket(value: Any) -> tuple[str, int]:
    seconds = _float_or_none(value)
    if seconds is None:
        return ("unknown", 999)
    if seconds < 60:
        return ("0-60", 0)
    if seconds < 180:
        return ("60-180", 1)
    if seconds < 420:
        return ("180-420", 2)
    if seconds <= 900:
        return ("420-900", 3)
    return ("900+", 4)


def _distance_from_strike_bucket(value: Any) -> tuple[str, int]:
    distance = _float_or_none(value)
    if distance is None:
        return ("unknown", 999)
    abs_distance = abs(distance)
    if abs_distance < 10:
        return ("very_close:<10", 0)
    if abs_distance < 25:
        return ("close:10-25", 1)
    if abs_distance < 75:
        return ("medium:25-75", 2)
    return ("far:75+", 3)


def _slope_at_entry_bucket(value: Any) -> tuple[str, int]:
    slope = _float_or_none(value)
    if slope is None:
        return ("unknown", 999)
    abs_slope = abs(slope)
    if abs_slope < 1:
        return ("weak:<1", 0)
    if abs_slope < 3:
        return ("medium:1-3", 1)
    return ("strong:3+", 2)


def _hold_seconds_bucket(value: Any) -> tuple[str, int]:
    seconds = _float_or_none(value)
    if seconds is None:
        return ("unknown", 999)
    if seconds < 60:
        return ("0-60", 0)
    if seconds < 180:
        return ("60-180", 1)
    if seconds < 420:
        return ("180-420", 2)
    return ("420+", 3)


def _open_positions(conn: sqlite3.Connection, *, has_predictions: bool) -> list[dict[str, Any]]:
    join = "LEFT JOIN predictions p ON p.id = t.prediction_id" if has_predictions else ""
    signal_expr = "COALESCE(p.action, t.side)" if has_predictions else "t.side"
    rows = conn.execute(
        f"""
        SELECT
            t.id AS trade_id,
            t.prediction_id,
            t.created_at,
            t.market_close_time,
            t.market_ticker,
            {signal_expr} AS signal,
            t.side,
            t.entry_price,
            t.contracts,
            t.notional,
            t.status,
            t.realized_pnl,
            t.exit_price,
            t.exit_reason
        FROM paper_trades t
        {join}
        WHERE t.status = 'OPEN'
        ORDER BY t.created_at DESC, t.id DESC
        """
    ).fetchall()
    return [_normalize_trade_row(row) for row in rows]


def _mark_open_positions(
    open_positions: list[dict[str, Any]],
    snapshot_path: Path | None,
    schema_gaps: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not open_positions:
        return open_positions, {
            "unrealized_pnl": 0.0,
            "marked_open_unrealized_pnl": 0.0,
            "marked_open_positions": 0,
            "unmarked_open_positions": 0,
            "unrealized_source": "no_open_positions",
        }

    if snapshot_path is None:
        schema_gaps.append("unrealized_pnl_unavailable: snapshot_path_not_configured")
        return open_positions, _unmarked_state(len(open_positions), "snapshot_path_not_configured")
    if not snapshot_path.exists():
        schema_gaps.append("unrealized_pnl_unavailable: realtime_snapshots_1s_db_missing")
        return open_positions, _unmarked_state(len(open_positions), "realtime_snapshots_1s_db_missing")

    marked = 0
    unmarked = 0
    unrealized_sum = 0.0
    try:
        with _connect_readonly(snapshot_path) as conn:
            tables = _table_names(conn)
            if REALTIME_SNAPSHOTS_TABLE not in tables:
                schema_gaps.append("unrealized_pnl_unavailable: realtime_snapshots_1s_table_missing")
                return open_positions, _unmarked_state(
                    len(open_positions), "realtime_snapshots_1s_table_missing"
                )
            snapshot_columns = _columns(conn, REALTIME_SNAPSHOTS_TABLE)
            required = {"ts", "market_ticker", "yes_bid", "no_bid"}
            missing = sorted(required - snapshot_columns)
            if missing:
                schema_gaps.append(
                    "unrealized_pnl_unavailable: realtime_snapshots_1s_missing_columns:"
                    + ",".join(missing)
                )
                return open_positions, _unmarked_state(
                    len(open_positions), "realtime_snapshots_1s_missing_columns"
                )

            for position in open_positions:
                side = str(position.get("side") or "").upper()
                price_column = "yes_bid" if side == "YES" else "no_bid" if side == "NO" else None
                if price_column is None:
                    position["mark_error"] = "invalid_side_for_mark"
                    unmarked += 1
                    continue
                snap = conn.execute(
                    """
                    SELECT ts, yes_bid, no_bid, yes_ask, no_ask
                    FROM realtime_snapshots_1s
                    WHERE market_ticker = ?
                    ORDER BY ts DESC
                    LIMIT 1
                    """,
                    (position.get("market_ticker"),),
                ).fetchone()
                if snap is None or snap[price_column] is None:
                    position["mark_error"] = "latest_snapshot_missing"
                    unmarked += 1
                    continue
                mark_price = float(snap[price_column])
                contracts = float(position.get("contracts") or 0.0)
                notional = float(position.get("notional") or 0.0)
                current_value = contracts * mark_price
                unrealized_pnl = current_value - notional
                position.update(
                    {
                        "mark_ts": snap["ts"],
                        "mark_price": mark_price,
                        "price_source": price_column,
                        "current_value": current_value,
                        "unrealized_pnl": unrealized_pnl,
                    }
                )
                unrealized_sum += unrealized_pnl
                marked += 1
    except sqlite3.Error as exc:
        schema_gaps.append(f"unrealized_pnl_unavailable: snapshot_sqlite_read_error:{exc}")
        return open_positions, _unmarked_state(len(open_positions), "snapshot_sqlite_read_error")

    if unmarked:
        schema_gaps.append(
            f"unrealized_pnl_partial: marked {marked} of {len(open_positions)} open positions"
        )
    return open_positions, {
        "unrealized_pnl": unrealized_sum if unmarked == 0 else None,
        "marked_open_unrealized_pnl": unrealized_sum,
        "marked_open_positions": marked,
        "unmarked_open_positions": unmarked,
        "unrealized_source": "realtime_snapshots_1s_bid" if unmarked == 0 else "partial_realtime_snapshots_1s_bid",
    }


def _unmarked_state(open_count: int, source: str) -> dict[str, Any]:
    return {
        "unrealized_pnl": None,
        "marked_open_unrealized_pnl": 0.0,
        "marked_open_positions": 0,
        "unmarked_open_positions": open_count,
        "unrealized_source": source,
    }


def _cumulative_pnl(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            COALESCE(settled_at, created_at) AS ts,
            id AS trade_id,
            market_ticker,
            side,
            realized_pnl AS pnl
        FROM paper_trades
        WHERE status IN ('SETTLED', 'CLOSED') AND realized_pnl IS NOT NULL
        ORDER BY COALESCE(settled_at, created_at), id
        """
    ).fetchall()
    running = 0.0
    points: list[dict[str, Any]] = []
    for row in rows:
        pnl = _float(row["pnl"])
        running += pnl
        points.append(
            {
                "ts": row["ts"],
                "trade_id": row["trade_id"],
                "market_ticker": row["market_ticker"],
                "side": row["side"],
                "pnl": pnl,
                "cumulative_pnl": running,
            }
        )
    return points[-max(1, int(limit)) :]


def _group_by_signal(
    conn: sqlite3.Connection,
    *,
    has_predictions: bool,
    limit: int,
) -> list[dict[str, Any]]:
    join = "LEFT JOIN predictions p ON p.id = t.prediction_id" if has_predictions else ""
    signal_expr = "COALESCE(p.action, t.side)" if has_predictions else "t.side"
    rows = conn.execute(
        f"""
        SELECT
            {signal_expr} AS signal,
            t.side AS side,
            COUNT(*) AS trades,
            COALESCE(SUM(CASE WHEN t.status = 'OPEN' THEN 1 ELSE 0 END), 0) AS open_positions,
            COALESCE(SUM(CASE WHEN t.status IN ('SETTLED', 'CLOSED') THEN 1 ELSE 0 END), 0) AS closed_positions,
            COALESCE(SUM(CASE WHEN t.status IN ('SETTLED', 'CLOSED') AND t.realized_pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
            COALESCE(SUM(CASE WHEN t.status IN ('SETTLED', 'CLOSED') AND t.realized_pnl IS NOT NULL THEN 1 ELSE 0 END), 0) AS closed_with_pnl,
            COALESCE(SUM(CASE WHEN t.status IN ('SETTLED', 'CLOSED') THEN t.realized_pnl ELSE 0 END), 0) AS realized_pnl,
            AVG(CASE WHEN t.status IN ('SETTLED', 'CLOSED') THEN t.realized_pnl END) AS avg_pnl,
            COALESCE(SUM(t.notional), 0) AS notional
        FROM paper_trades t
        {join}
        GROUP BY {signal_expr}, t.side
        ORDER BY realized_pnl DESC, trades DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    return [_normalize_group_row(row) for row in rows]


def _group_by_market(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            market_ticker,
            COUNT(*) AS trades,
            COALESCE(SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END), 0) AS open_positions,
            COALESCE(SUM(CASE WHEN status IN ('SETTLED', 'CLOSED') THEN 1 ELSE 0 END), 0) AS closed_positions,
            COALESCE(SUM(CASE WHEN status IN ('SETTLED', 'CLOSED') AND realized_pnl > 0 THEN 1 ELSE 0 END), 0) AS wins,
            COALESCE(SUM(CASE WHEN status IN ('SETTLED', 'CLOSED') AND realized_pnl IS NOT NULL THEN 1 ELSE 0 END), 0) AS closed_with_pnl,
            COALESCE(SUM(CASE WHEN status IN ('SETTLED', 'CLOSED') THEN realized_pnl ELSE 0 END), 0) AS realized_pnl,
            AVG(CASE WHEN status IN ('SETTLED', 'CLOSED') THEN realized_pnl END) AS avg_pnl,
            COALESCE(SUM(notional), 0) AS notional
        FROM paper_trades
        GROUP BY market_ticker
        ORDER BY MIN(created_at) DESC
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    return [_normalize_group_row(row) for row in rows]


def _normalize_trade_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in (
        "entry_price",
        "exit_price",
        "contracts",
        "notional",
        "realized_pnl",
        "edge",
        "confidence",
        "official_expiration_value",
        "settlement_value_dollars",
    ):
        if key in data:
            data[key] = _float_or_none(data[key])
    data["settlement_source_label"] = _settlement_source_label(data.get("settlement_source"))
    return data


def _settlement_source_label(value: Any) -> str | None:
    if value is None or value == "":
        return None
    source = str(value)
    if source == "kalshi_official":
        return "official (Kalshi)"
    if source == "coinbase_estimate":
        return "estimated (Coinbase/raw)"
    return source


def _normalize_group_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    closed_with_pnl = _int(data.get("closed_with_pnl"))
    wins = _int(data.get("wins"))
    data["trades"] = _int(data.get("trades"))
    data["open_positions"] = _int(data.get("open_positions"))
    data["closed_positions"] = _int(data.get("closed_positions"))
    data["wins"] = wins
    data["closed_with_pnl"] = closed_with_pnl
    data["win_rate"] = (wins / closed_with_pnl) if closed_with_pnl else 0.0
    data["realized_pnl"] = _float(data.get("realized_pnl"))
    data["avg_pnl"] = _float_or_none(data.get("avg_pnl"))
    data["notional"] = _float(data.get("notional"))
    return data


def _json_mapping(value: Any) -> dict[str, Any]:
    parsed = _json_value(value)
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _review_reason(value: Any) -> str | None:
    parsed = _json_value(value)
    if isinstance(parsed, str):
        return parsed or None
    if isinstance(parsed, Mapping):
        return _first_text(parsed, "reason", "message", "text", "label")
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, str) and item:
                return item
            if isinstance(item, Mapping):
                text = _first_text(item, "reason", "message", "text", "label")
                if text:
                    return text
    return None


def _json_value(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _first_text(mapping: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _first_float(mapping: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float_or_none(mapping.get(key))
        if value is not None:
            return value
    return None


def _seconds_between(start: Any, end: Any) -> float | None:
    start_dt = _parse_datetime(start)
    end_dt = _parse_datetime(end)
    if start_dt is None or end_dt is None:
        return None
    try:
        return max(0.0, (end_dt - start_dt).total_seconds())
    except TypeError:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _float(value: Any) -> float:
    parsed = _float_or_none(value)
    return 0.0 if parsed is None else parsed


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
