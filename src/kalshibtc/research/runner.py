"""Replay-only candidate research runner CLI."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import BotConfig, RiskLimits
from ..execution.risk import RiskManager
from ..replay.cli import (
    _load_official_settlement_rows,
    _load_snapshot_rows,
    _metrics_payload,
    _rows_to_replay_inputs,
    _write_portfolio_settlement,
    _write_results,
)
from ..replay.replay import ReplayEngine
from ..replay.settlement import compute_portfolio_settlement
from ..research.gates import ResearchGateThresholds, evaluate_research_gates
from ..research.specs import SpecValidationError, load_candidate_spec
from ..strategy.parameterized_late_window import strategy_from_candidate_spec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kbtc-research",
        description="Run replay-only candidate strategy specs and write research artifacts.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="Run one candidate spec through replay")
    run_parser.add_argument("--spec", required=True, help="Candidate spec JSON path")
    run_parser.add_argument("--feed-db", required=True, help="Read-only feed SQLite DB path")
    run_parser.add_argument("--runs-dir", default="runs/research", help="Research runs root")
    run_parser.add_argument("--research-dir", default="research", help="Research artifact root")
    run_parser.add_argument("--run-id", default=None, help="Run ID. Defaults to timestamp.")
    run_parser.add_argument("--from", dest="from_ts", default=None, help="Inclusive ISO timestamp lower bound")
    run_parser.add_argument("--to", dest="to_ts", default=None, help="Inclusive ISO timestamp upper bound")
    run_parser.add_argument("--json", action="store_true", help="Print summary JSON")
    run_parser.add_argument("--min-trades", type=int, default=10)
    run_parser.add_argument("--max-drawdown", type=float, default=None)
    run_parser.add_argument("--min-sharpe", type=float, default=None)
    run_parser.add_argument("--min-sharpe-trades", type=int, default=30)
    args = parser.parse_args(argv)

    if args.command == "run":
        return _run_candidate(args)
    parser.error(f"unknown command: {args.command}")
    return 2


def _run_candidate(args: argparse.Namespace) -> int:
    spec_path = Path(args.spec)
    feed_db = Path(args.feed_db)
    try:
        spec = load_candidate_spec(spec_path.read_text(encoding="utf-8"))
        strategy = strategy_from_candidate_spec(spec)
    except (OSError, SpecValidationError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    run_id = args.run_id or datetime.now(tz=UTC).strftime("research-%Y%m%dT%H%M%SZ")
    run_dir = Path(args.runs_dir) / strategy.name / run_id
    if run_dir.exists():
        print(f"run directory already exists: {run_dir}", file=sys.stderr)
        return 2
    run_dir.mkdir(parents=True, exist_ok=False)

    rows = _load_snapshot_rows(feed_db, from_ts=args.from_ts, to_ts=args.to_ts)
    ticks, books, contract, reference_provenance = _rows_to_replay_inputs(rows)
    report = ReplayEngine(
        config=BotConfig(),
        contract=contract,
        strategies=[strategy],
        risk_manager=RiskManager(
            RiskLimits(
                base_size_dollars=float(spec.parameters.get("target_notional") or 25.0),
                max_position_dollars=float(spec.parameters.get("target_notional") or 25.0),
                max_open_positions=int(spec.parameters.get("max_open_positions") or 1),
                max_spread=float(spec.parameters.get("max_entry_spread") or 0.05),
            )
        ),
        settle_on_market_rollover=True,
        fill_timing="next-tick",
    ).run(ticks=ticks, books=books)

    results_db = run_dir / "results.sqlite3"
    _write_results(results_db, report.results, strategies=[strategy])
    official_rows = _load_official_settlement_rows(feed_db)
    settlement_rows = official_rows or rows
    portfolio_settlement = compute_portfolio_settlement(
        report.fills,
        settlement_rows,
        position_mode="kalshi_single_position",
    )
    _write_portfolio_settlement(results_db, portfolio_settlement)
    (run_dir / "portfolio_settlement.json").write_text(
        json.dumps(portfolio_settlement["aggregate"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (run_dir / "candidate_spec.json").write_text(spec.to_canonical_json() + "\n", encoding="utf-8")
    _write_config(run_dir / "config.toml", args=args, spec_name=spec.name, strategy_name=strategy.name, run_id=run_id)

    metrics = _metrics_payload(
        report=report,
        feed_db=feed_db,
        run_dir=run_dir,
        strategy=strategy.name,
        max_open_positions=int(spec.parameters.get("max_open_positions") or 1),
        settlement_rows=settlement_rows,
        strategies=[strategy],
        portfolio_settlement=portfolio_settlement,
        venue="kalshi",
        fill_timing="next-tick",
        settlements_from_feed_db=bool(official_rows),
        reference_price_source="single_venue",
        reference_provenance=reference_provenance,
    )
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    gates = evaluate_research_gates(
        metrics["institutional_metrics"],
        ResearchGateThresholds(
            min_trades=args.min_trades,
            max_drawdown=args.max_drawdown,
            min_sharpe=args.min_sharpe,
            min_sharpe_trades=args.min_sharpe_trades,
        ),
    )
    summary = {
        "candidate": spec.to_dict(),
        "run_dir": str(run_dir),
        "report_path": str(run_dir / "research_summary.json"),
        "metrics": metrics,
        "gates": gates,
        "safety_boundary": "replay/research-only; no live orders",
    }
    (run_dir / "research_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _append_manifest(Path(args.research_dir) / "candidate_runs" / "manifest.csv", summary)
    if args.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print(f"kbtc_research run_id={run_id} run_dir={run_dir} passed={gates['passed']}")
    return 0


def _write_config(path: Path, *, args: argparse.Namespace, spec_name: str, strategy_name: str, run_id: str) -> None:
    path.write_text(
        "\n".join(
            [
                f'feed_db = "{Path(args.feed_db)}"',
                f'strategy = "{strategy_name}"',
                f'candidate_spec = "{Path(args.spec)}"',
                f'candidate_name = "{spec_name}"',
                f'run_id = "{run_id}"',
                f'from_ts = "{args.from_ts or ""}"',
                f'to_ts = "{args.to_ts or ""}"',
                'fill_timing = "next-tick"',
                "safety_boundary = \"replay/research-only; no live orders\"",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _append_manifest(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["created_at", "candidate", "run_id", "passed", "total_pnl", "run_dir"],
            lineterminator="\n",
        )
        if not exists:
            writer.writeheader()
        metrics = summary["metrics"]["institutional_metrics"]
        writer.writerow(
            {
                "created_at": datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "candidate": summary["candidate"]["name"],
                "run_id": Path(summary["run_dir"]).name,
                "passed": str(bool(summary["gates"]["passed"])).lower(),
                "total_pnl": metrics.get("total_pnl", 0.0),
                "run_dir": summary["run_dir"],
            }
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
