from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..backtest.metrics import compute_metrics
from ..config import BotConfig, RiskLimits
from ..datafeed.composite_reference import VenueObservation, compute_composite_reference
from ..datafeed.models import OrderBookSnapshot, Tick
from ..execution.paper import PaperFill
from ..execution.risk import RiskDecision, RiskManager
from ..market.contract import ContractWindow
from ..runtime_paths import DEFAULT_FEED_DB, DEFAULT_RUNS_DIR, resolve_feed_db, resolve_runs_dir
from ..strategy.registry import create_strategy, strategy_names
from ..strategy.signals import Signal
from .replay import ReplayEngine
from .settlement import (
    backfill_market_settlements as backfill_market_settlements,
)
from .settlement import (
    estimate_replay_fill_pnls,
    load_official_market_settlements,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kbtc-replay",
        description="Replay a recorded Kalshi BTC 1s feed DB through a strategy into an immutable run directory.",
    )
    parser.add_argument("--feed-db", default=None, help=f"Feed SQLite DB. Default: {DEFAULT_FEED_DB}")
    parser.add_argument("--runs-dir", default=None, help=f"Runs root directory. Default: {DEFAULT_RUNS_DIR}")
    parser.add_argument("--strategy", default="simple_directional", choices=strategy_names())
    parser.add_argument("--run-id", default=None, help="Run directory name. Defaults to current UTC timestamp.")
    parser.add_argument("--from", dest="from_ts", default=None, help="Inclusive ISO timestamp lower bound.")
    parser.add_argument("--to", dest="to_ts", default=None, help="Inclusive ISO timestamp upper bound.")
    parser.add_argument("--base-size-dollars", type=float, default=25.0)
    parser.add_argument("--max-position-dollars", type=float, default=25.0)
    parser.add_argument("--max-open-positions", type=int, default=1)
    parser.add_argument("--max-spread", type=float, default=0.05)
    parser.add_argument(
        "--no-settle-on-market-rollover",
        action="store_true",
        help="Do not reset replay open-position count when the market ticker changes.",
    )
    parser.add_argument(
        "--reference-price-source",
        choices=("single_venue", "composite_60s_reference"),
        default="single_venue",
        help="Replay distance-to-strike with raw single-venue BTC price (default) or local composite 60s approximation.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON summary.")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing an existing run directory.")
    args = parser.parse_args(argv)

    feed_db = resolve_feed_db(args.feed_db)
    runs_dir = resolve_runs_dir(args.runs_dir)
    run_id = args.run_id or datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = runs_dir / args.strategy / run_id
    if run_dir.exists() and not args.overwrite:
        print(f"run directory already exists: {run_dir}", file=sys.stderr)
        return 2
    run_dir.mkdir(parents=True, exist_ok=True)

    rows = _load_snapshot_rows(feed_db, from_ts=args.from_ts, to_ts=args.to_ts)
    ticks, books, contract, reference_provenance = _rows_to_replay_inputs(
        rows,
        reference_price_source=args.reference_price_source,
    )
    strategy = create_strategy(args.strategy)
    risk = RiskManager(
        RiskLimits(
            base_size_dollars=args.base_size_dollars,
            max_position_dollars=args.max_position_dollars,
            max_open_positions=args.max_open_positions,
            max_spread=args.max_spread,
        )
    )
    report = ReplayEngine(
        config=BotConfig(),
        contract=contract,
        strategies=[strategy],
        risk_manager=risk,
        settle_on_market_rollover=not args.no_settle_on_market_rollover,
    ).run(ticks=ticks, books=books)

    results_db = run_dir / "results.sqlite3"
    _write_results(results_db, report.results)
    config_text = _config_text(args=args, feed_db=feed_db, run_id=run_id)
    (run_dir / "config.toml").write_text(config_text, encoding="utf-8")
    metrics = _metrics_payload(
        report=report,
        feed_db=feed_db,
        run_dir=run_dir,
        strategy=args.strategy,
        max_open_positions=args.max_open_positions,
        settlement_rows=rows,
        official_settlements=load_official_market_settlements(feed_db),
        reference_price_source=args.reference_price_source,
        reference_provenance=reference_provenance,
    )
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(metrics, sort_keys=True))
    else:
        print(
            "kbtc_replay "
            f"strategy={args.strategy} snapshots={metrics['snapshots']} "
            f"signals={metrics['signals']} fills={metrics['fills']} run_dir={run_dir}"
        )
    return 0


def _load_snapshot_rows(feed_db: Path, *, from_ts: str | None, to_ts: str | None) -> list[sqlite3.Row]:
    if not feed_db.exists():
        raise FileNotFoundError(f"feed DB not found: {feed_db}")
    where: list[str] = []
    params: list[str] = []
    if from_ts:
        where.append("ts >= ?")
        params.append(from_ts)
    if to_ts:
        where.append("ts <= ?")
        params.append(to_ts)
    sql = "SELECT * FROM realtime_snapshots_1s"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts ASC, market_ticker ASC"
    with sqlite3.connect(f"file:{feed_db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return list(conn.execute(sql, params))


def _rows_to_replay_inputs(
    rows: list[sqlite3.Row],
    *,
    reference_price_source: str = "single_venue",
) -> tuple[list[Tick], list[OrderBookSnapshot], ContractWindow, dict[str, Any]]:
    if not rows:
        raise ValueError("feed DB query returned no snapshots")
    first = rows[0]
    contract = ContractWindow(
        ticker=str(first["market_ticker"]),
        strike=float(first["strike"]),
        close_time=_parse_dt(first["market_close_time"]),
        open_time=_parse_dt(first["market_open_time"]) if _has_column(first, "market_open_time") and first["market_open_time"] else None,
    )
    ticks: list[Tick] = []
    books: list[OrderBookSnapshot] = []
    provenance: dict[str, Any] = {
        "requested_source": reference_price_source,
        "single_venue_ticks": 0,
        "composite_60s_reference_ticks": 0,
        "warnings": [],
        "label": "approximation_not_official_cf_or_kalshi"
        if reference_price_source == "composite_60s_reference"
        else "raw_single_venue_feed_price",
    }
    for row in rows:
        ts = _parse_dt(row["ts"])
        raw = _json_or_empty(row["raw_json"] if _has_column(row, "raw_json") else None)
        raw_price = float(row["btc_price"])
        reference_price = raw_price
        source = "single_venue"
        reference = None
        if reference_price_source == "composite_60s_reference":
            reference = compute_composite_reference(_venue_observations_from_raw(raw), as_of=ts)
            if reference is not None and reference.source == "composite_60s_reference":
                reference_price = reference.price
                source = reference.source
            elif reference is not None and reference.source == "single_venue":
                provenance["warnings"].extend(reference.provenance.get("warnings", []))
        provenance[f"{source}_ticks"] = int(provenance.get(f"{source}_ticks", 0)) + 1
        tick_raw = dict(raw)
        tick_raw["reference_price_source"] = source
        tick_raw["reference_price"] = reference_price
        tick_raw["raw_btc_price"] = raw_price
        if reference is not None:
            tick_raw["reference_price_provenance"] = reference.provenance
        ticks.append(
            Tick(
                ts=ts,
                price=reference_price,
                source=source,
                symbol="BTC-USD",
                raw=tick_raw,
            )
        )
        books.append(
            OrderBookSnapshot(
                ts=ts,
                market_ticker=str(row["market_ticker"]),
                yes_bid=_optional_float(row, "yes_bid"),
                yes_ask=_optional_float(row, "yes_ask"),
                no_bid=_optional_float(row, "no_bid"),
                no_ask=_optional_float(row, "no_ask"),
                sequence=_optional_int(row, "orderbook_sequence"),
                raw=raw,
            )
        )
    provenance["warnings"] = sorted(set(provenance["warnings"]))
    return ticks, books, contract, provenance


def _write_results(path: Path, results: list[Any]) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE replay_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                strategy TEXT NOT NULL,
                side TEXT NOT NULL,
                confidence REAL NOT NULL,
                reason TEXT NOT NULL,
                allowed INTEGER NOT NULL,
                blocked_by_json TEXT NOT NULL,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE replay_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                strategy TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                contracts REAL NOT NULL,
                notional REAL NOT NULL,
                raw_json TEXT NOT NULL
            );
            """
        )
        for result in results:
            state = getattr(result, "state", None)
            tick = getattr(state, "tick", None)
            signal: Signal = result.signal
            risk: RiskDecision = result.risk
            fill: PaperFill | None = result.fill
            ts = fill.ts.isoformat() if fill is not None else ""
            raw = {
                "signal": signal.__dict__,
                "risk": {
                    "allowed": risk.allowed,
                    "size_dollars": risk.size_dollars,
                    "entry_price": risk.entry_price,
                    "blocked_by": risk.blocked_by,
                },
                "state": {
                    "reference_price": state.price if state is not None else None,
                    "reference_price_source": tick.source if tick is not None else None,
                    "raw_btc_price": tick.raw.get("raw_btc_price") if tick is not None else None,
                },
            }
            conn.execute(
                """
                INSERT INTO replay_signals (
                    ts, strategy, side, confidence, reason, allowed, blocked_by_json, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    signal.strategy,
                    signal.side,
                    signal.confidence,
                    signal.reason,
                    1 if risk.allowed else 0,
                    json.dumps(risk.blocked_by, sort_keys=True),
                    json.dumps(raw, sort_keys=True),
                ),
            )
            if fill is not None:
                conn.execute(
                    """
                    INSERT INTO replay_fills (
                        ts, strategy, side, entry_price, contracts, notional, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fill.ts.isoformat(),
                        signal.strategy,
                        fill.side,
                        fill.entry_price,
                        fill.contracts,
                        fill.notional,
                        json.dumps(fill.__dict__, default=str, sort_keys=True),
                    ),
                )


def _metrics_payload(
    *,
    report: Any,
    feed_db: Path,
    run_dir: Path,
    strategy: str,
    max_open_positions: int,
    settlement_rows: Sequence[Mapping[str, Any] | Any] | None = None,
    official_settlements: Mapping[str, Mapping[str, Any]] | None = None,
    reference_price_source: str = "single_venue",
    reference_provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    settled_fills = estimate_replay_fill_pnls(
        report.fills,
        settlement_rows or [],
        official_settlements=official_settlements,
    )
    institutional_metrics: dict[str, Any] = dict(compute_metrics(settled_fills))
    settlement_sources = sorted(
        {
            str(fill.get("settlement_source"))
            for fill in settled_fills
            if fill.get("settlement_source")
        }
    )
    if settlement_sources:
        institutional_metrics["settlement_sources"] = settlement_sources
        institutional_metrics["settlement_source"] = (
            settlement_sources[0] if len(settlement_sources) == 1 else "mixed"
        )
    institutional_metrics["settled_trades"] = sum(
        1
        for fill in settled_fills
        if fill.get("settlement_source") in {"kalshi_official", "replay_final_snapshot"}
    )
    return {
        "feed_db": str(feed_db),
        "run_dir": str(run_dir),
        "run_id": run_dir.name,
        "strategy": strategy,
        "snapshots": report.total_ticks,
        "signals": report.total_signals,
        "fills": len(report.fills),
        "settled_positions": getattr(report, "settled_positions", 0),
        "notional": round(sum(fill.notional for fill in report.fills), 6),
        "max_open_positions": max_open_positions,
        "reference_price_source": reference_price_source,
        "reference_price_provenance": dict(reference_provenance or {}),
        "institutional_metrics": institutional_metrics,
    }


def _config_text(*, args: argparse.Namespace, feed_db: Path, run_id: str) -> str:
    return "\n".join(
        [
            f'feed_db = "{feed_db}"',
            f'strategy = "{args.strategy}"',
            f'run_id = "{run_id}"',
            f'from_ts = "{args.from_ts or ""}"',
            f'to_ts = "{args.to_ts or ""}"',
            f"base_size_dollars = {args.base_size_dollars}",
            f"max_position_dollars = {args.max_position_dollars}",
            f"max_open_positions = {args.max_open_positions}",
            f"max_spread = {args.max_spread}",
            f'reference_price_source = "{args.reference_price_source}"',
            f"settle_on_market_rollover = {str(not args.no_settle_on_market_rollover).lower()}",
            "",
        ]
    )


def _venue_observations_from_raw(raw: Mapping[str, Any]) -> list[VenueObservation]:
    payload = raw.get("btc_venue_observations") or raw.get("venue_observations") or []
    if not isinstance(payload, list):
        return []
    observations: list[VenueObservation] = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        venue = item.get("venue") or item.get("exchange") or item.get("source")
        ts = item.get("ts") or item.get("timestamp")
        price = item.get("price")
        if price is None:
            price = item.get("btc_price")
        if venue is None or ts is None or price is None:
            continue
        try:
            observations.append(VenueObservation(venue=str(venue), ts=_parse_dt(ts), price=float(price)))
        except (TypeError, ValueError):
            continue
    return observations


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _has_column(row: sqlite3.Row, name: str) -> bool:
    return name in row.keys()


def _optional_float(row: sqlite3.Row, name: str) -> float | None:
    if not _has_column(row, name) or row[name] is None:
        return None
    return float(row[name])


def _optional_int(row: sqlite3.Row, name: str) -> int | None:
    if not _has_column(row, name) or row[name] is None:
        return None
    return int(row[name])


def _json_or_empty(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


if __name__ == "__main__":
    raise SystemExit(main())
