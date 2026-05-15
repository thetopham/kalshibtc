from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

CLOSED_STATUSES = {"CLOSED", "SETTLED"}
PAPER_TRADES_TABLE = "paper_trades"
PREDICTIONS_TABLE = "predictions"
REALTIME_SNAPSHOTS_TABLE = "realtime_snapshots_1s"


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

            metrics = _summary_metrics(conn, unrealized_state)
            payload.update(
                {
                    "metrics": metrics,
                    "open_positions": open_positions,
                    "recent_trades": _recent_trades(
                        conn,
                        has_predictions=has_predictions,
                        limit=recent_limit,
                    ),
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
        "cumulative_pnl": [],
        "by_signal": [],
        "by_market": [],
    }


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
    limit: int,
) -> list[dict[str, Any]]:
    join = "LEFT JOIN predictions p ON p.id = t.prediction_id" if has_predictions else ""
    signal_expr = "COALESCE(p.action, t.side)" if has_predictions else "t.side"
    edge_expr = "p.edge" if has_predictions else "NULL"
    confidence_expr = "p.confidence" if has_predictions else "NULL"
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
    ):
        if key in data:
            data[key] = _float_or_none(data[key])
    return data


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
