from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize volatility_hedge live-paper position quality.")
    parser.add_argument("--db", required=True, type=Path)
    args = parser.parse_args(argv)
    summary = summarize(args.db)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def summarize(db: Path) -> dict[str, Any]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if not _table_exists(conn, "volatility_hedge_positions"):
            return {"positions": 0, "error": "missing volatility_hedge_positions"}
        positions = conn.execute("SELECT * FROM volatility_hedge_positions").fetchall()
        events = _table_count(conn, "volatility_hedge_events")
        invalid = _count_where(conn, "volatility_hedge_events", "reason = 'invalid_book'")
        fills = _count_where(conn, "volatility_hedge_events", "event_type = 'fill'")
        phase_counts = _group_count(conn, "volatility_hedge_events", "lifecycle_phase")
        fills_by_phase = _group_count(conn, "volatility_hedge_events", "lifecycle_phase", where="event_type = 'fill'")
        rejections_by_phase = _group_count(conn, "volatility_hedge_events", "lifecycle_phase", where="event_type = 'decision'")
        current_phase_by_market = _current_phase_by_market(conn)
    total_events = max(events, 1)
    return {
        "positions": len(positions),
        "events": events,
        "fills": fills,
        "invalid_book_count": invalid,
        "invalid_book_rate": invalid / total_events,
        "paired_qty": _sum(positions, "paired_qty"),
        "locked_edge": _sum(positions, "locked_edge_dollars"),
        "settlement_EV": _sum(positions, "settlement_EV"),
        "worst_case_pnl_sum": _sum(positions, "worst_case_pnl"),
        "max_imbalance_ratio": max([float(row["imbalance_ratio"] or 0.0) for row in positions], default=0.0),
        "repair_qty_needed": _sum(positions, "repair_qty_needed"),
        "residual_up_qty": _sum(positions, "residual_up_qty"),
        "residual_down_qty": _sum(positions, "residual_down_qty"),
        "modes": _counts(positions, "mode"),
        "phase_counts": phase_counts,
        "fills_by_phase": fills_by_phase,
        "rejections_by_phase": rejections_by_phase,
        "current_phase_by_market": current_phase_by_market,
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _count_where(conn: sqlite3.Connection, table: str, where: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0])


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if not _table_exists(conn, table):
        return False
    return any(str(row[1]) == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _group_count(conn: sqlite3.Connection, table: str, column: str, *, where: str | None = None) -> dict[str, int]:
    if not _column_exists(conn, table, column):
        return {}
    sql = f"SELECT COALESCE({column}, 'UNKNOWN') AS bucket, COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    sql += f" GROUP BY {column}"
    return {str(row[0]): int(row[1]) for row in conn.execute(sql).fetchall()}


def _current_phase_by_market(conn: sqlite3.Connection) -> dict[str, str]:
    if not _column_exists(conn, "volatility_hedge_positions", "lifecycle_phase"):
        return {}
    rows = conn.execute(
        """
        SELECT market_ticker, lifecycle_phase
        FROM volatility_hedge_positions
        WHERE lifecycle_phase IS NOT NULL
        ORDER BY updated_at DESC
        """
    ).fetchall()
    phases: dict[str, str] = {}
    for row in rows:
        phases.setdefault(str(row[0]), str(row[1]))
    return phases


def _sum(rows: list[sqlite3.Row], column: str) -> float:
    return round(sum(float(row[column] or 0.0) for row in rows if column in row.keys()), 10)


def _counts(rows: list[sqlite3.Row], column: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row[column] or "UNKNOWN") if column in row.keys() else "UNKNOWN"
        counts[value] = counts.get(value, 0) + 1
    return counts


if __name__ == "__main__":
    raise SystemExit(main())
