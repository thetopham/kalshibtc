#!/usr/bin/env python3
"""Replay every venue-eligible strategy split by venue and strategy.

Read-only: consumes local Kalshi/Polymarket 1s feed SQLite DBs and writes a
comparison artifact under runs/strategy_comparisons/. Venue-ineligible
strategies are skipped, e.g. Polymarket-only inventory hedging strategies are
not replayed on Kalshi.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import math
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kalshibtc.strategy.registry import strategy_metadata, strategy_names  # noqa: E402

DEFAULT_FEEDS = {
    "kalshi": ROOT / "feed" / "kalshi-btc-1s.sqlite3",
    "polymarket": ROOT / "feed" / "polymarket-btc-1s.sqlite3",
}
VENUES = ("kalshi", "polymarket")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run read-only replay matrix split by venue and strategy."
    )
    parser.add_argument("--runs-dir", type=Path, default=ROOT / "runs")
    parser.add_argument("--kalshi-feed-db", type=Path, default=DEFAULT_FEEDS["kalshi"])
    parser.add_argument("--polymarket-feed-db", type=Path, default=DEFAULT_FEEDS["polymarket"])
    parser.add_argument("--from", dest="from_ts", default=None)
    parser.add_argument("--to", dest="to_ts", default=None)
    parser.add_argument(
        "--row-limit",
        type=int,
        default=None,
        help="Optional smoke/screening cap per venue. Uses earliest rows in the selected window.",
    )
    parser.add_argument("--workers", type=int, default=None, help="Parallel replay workers. Default reserves --reserve-cpus cores for live 1s feeds and system services.")
    parser.add_argument("--reserve-cpus", type=int, default=4, help="CPU cores to leave unused by replay workers; protects live datafeed recorders.")
    parser.add_argument("--child-nice", type=int, default=10, help="Nice value applied to child replay processes on POSIX so datafeeds stay responsive.")
    parser.add_argument(
        "--strategy",
        action="append",
        default=[],
        choices=strategy_names(),
        help="Limit to one or more strategies. Default: all registered strategies.",
    )
    parser.add_argument(
        "--venue",
        action="append",
        default=[],
        choices=VENUES,
        help="Limit to one or more venues. Default: kalshi and polymarket.",
    )
    parser.add_argument("--base-size-dollars", type=float, default=25.0)
    parser.add_argument("--max-position-dollars", type=float, default=5000.0)
    parser.add_argument("--max-open-positions", type=int, default=100000)
    parser.add_argument("--max-spread", type=float, default=1.0)
    parser.add_argument("--starting-bankroll", type=float, default=100000.0)
    parser.add_argument("--max-capital-at-risk", type=float, default=100000.0)
    parser.add_argument("--per-market-max-exposure", type=float, default=5000.0)
    parser.add_argument("--fee-per-contract", type=float, default=0.0)
    parser.add_argument("--fee-rate", type=float, default=0.0)
    parser.add_argument("--run-prefix", default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    started = datetime.now(tz=UTC)
    run_prefix = args.run_prefix or f"venue-strategy-matrix-{started.strftime('%Y%m%dT%H%M%SZ')}"
    selected_venues = tuple(args.venue or VENUES)
    selected_strategies = tuple(args.strategy or strategy_names())
    feeds = {"kalshi": args.kalshi_feed_db, "polymarket": args.polymarket_feed_db}
    workers = effective_workers(args.workers, reserve_cpus=args.reserve_cpus)

    venue_windows = {
        venue: selected_window(feeds[venue], from_ts=args.from_ts, to_ts=args.to_ts, row_limit=args.row_limit)
        for venue in selected_venues
    }
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    total = sum(
        1
        for venue in selected_venues
        for strategy in selected_strategies
        if venue in strategy_metadata(strategy).allowed_venues
    )
    done = 0
    print(
        f"strategy venue matrix start venues={selected_venues} strategies={len(selected_strategies)} eligible_runs={total}",
        flush=True,
    )

    jobs: list[dict[str, Any]] = []
    for venue in selected_venues:
        feed_db = feeds[venue]
        window = venue_windows[venue]
        for strategy in selected_strategies:
            metadata = strategy_metadata(strategy)
            if venue not in metadata.allowed_venues:
                skipped.append(
                    {
                        "venue": venue,
                        "strategy": strategy,
                        "reason": "venue_ineligible",
                        "allowed_venues": ",".join(metadata.allowed_venues),
                    }
                )
                continue
            run_id = f"{run_prefix}-{venue}-{strategy}"
            jobs.append({"venue": venue, "strategy": strategy, "feed_db": feed_db, "run_id": run_id, "window": window})

    print(
        f"parallel replay workers={workers} reserve_cpus={args.reserve_cpus} child_nice={args.child_nice}",
        flush=True,
    )
    if workers <= 1:
        for job in jobs:
            row = run_one(args, **job)
            results.append(row)
            done += 1
            print_progress(done, total, row)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_job = {executor.submit(run_one, args, **job): job for job in jobs}
            for future in concurrent.futures.as_completed(future_to_job):
                row = future.result()
                results.append(row)
                done += 1
                print_progress(done, total, row)

    results.sort(key=lambda r: (r.get("venue", ""), r.get("strategy", "")))
    finished = datetime.now(tz=UTC)
    valid = [r for r in results if not r.get("error")]
    for row in valid:
        pair = float(row.get("completed_pair_pnl", 0.0) or 0.0)
        unpaired = float(row.get("unpaired_pnl", 0.0) or 0.0)
        row["risk_adjusted_score"] = round(
            float(row.get("realized_pnl", 0.0) or 0.0)
            - 0.25 * abs(unpaired)
            - 0.10 * float(row.get("max_drawdown", 0.0) or 0.0),
            6,
        )
        row["leakage_ok_75pct"] = bool(pair > 0 and unpaired >= -0.75 * max(pair, 1.0))

    artifact = {
        "created_at": finished.isoformat(),
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": (finished - started).total_seconds(),
        "scope": "Read-only replay of every strategy split by venue and strategy; venue-ineligible strategies are skipped.",
        "safety_boundary": "replay/backtest only; no order submission",
        "run_prefix": run_prefix,
        "worker_controls": {"workers": workers, "reserve_cpus": args.reserve_cpus, "cpu_count": os.cpu_count(), "child_nice": args.child_nice},
        "feeds": {venue: str(feeds[venue]) for venue in selected_venues},
        "window_request": {"from": args.from_ts, "to": args.to_ts, "row_limit": args.row_limit},
        "venue_windows": venue_windows,
        "strategy_count_requested": len(selected_strategies),
        "eligible_run_count": total,
        "valid_run_count": len(valid),
        "error_count": len(results) - len(valid),
        "skipped_count": len(skipped),
        "skipped": skipped,
        "top_by_raw_pnl": sorted(valid, key=lambda r: (r.get("realized_pnl", -10**9), r.get("roi", 0.0)), reverse=True),
        "top_by_risk_adjusted_score": sorted(valid, key=lambda r: r.get("risk_adjusted_score", -10**9), reverse=True),
        "best_by_venue": {
            venue: sorted(
                [r for r in valid if r["venue"] == venue],
                key=lambda r: (r.get("realized_pnl", -10**9), r.get("roi", 0.0)),
                reverse=True,
            )[:10]
            for venue in selected_venues
        },
        "best_by_strategy": {
            strategy: sorted(
                [r for r in valid if r["strategy"] == strategy],
                key=lambda r: (r.get("realized_pnl", -10**9), r.get("roi", 0.0)),
                reverse=True,
            )
            for strategy in selected_strategies
        },
        "errors": [r for r in results if r.get("error")],
        "all_results": results,
    }

    out_stem = args.out or args.runs_dir / "strategy_comparisons" / run_prefix
    out_stem.parent.mkdir(parents=True, exist_ok=True)
    json_path = out_stem.with_suffix(".json")
    csv_path = out_stem.with_suffix(".csv")
    md_path = out_stem.with_suffix(".md")
    json_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(csv_path, valid)
    write_md(md_path, artifact)

    payload = {
        "json": str(json_path),
        "csv": str(csv_path),
        "md": str(md_path),
        "valid_run_count": len(valid),
        "error_count": artifact["error_count"],
        "skipped_count": len(skipped),
        "top": artifact["top_by_raw_pnl"][:5],
    }
    print(json.dumps(payload, sort_keys=True) if args.json else payload)
    return 0 if not artifact["error_count"] else 1


def effective_workers(requested: int | None, *, reserve_cpus: int) -> int:
    cpu_count = os.cpu_count() or 1
    if requested is not None:
        return max(1, min(int(requested), cpu_count))
    return max(1, cpu_count - max(0, reserve_cpus))


def child_preexec(nice_value: int):
    if os.name != "posix" or nice_value <= 0:
        return None

    def _preexec() -> None:
        try:
            os.nice(nice_value)
        except OSError:
            pass

    return _preexec


def print_progress(done: int, total: int, row: dict[str, Any]) -> None:
    print(
        f"progress {done}/{total} venue={row.get('venue')} strategy={row.get('strategy')} pnl={row.get('realized_pnl')} fills={row.get('fills')} error={row.get('error')}",
        flush=True,
    )


def run_one(
    args: argparse.Namespace,
    *,
    venue: str,
    strategy: str,
    feed_db: Path,
    run_id: str,
    window: dict[str, Any],
) -> dict[str, Any]:
    if strategy == "hedge_volatility_v0":
        cmd = [
            sys.executable,
            "-m",
            "kalshibtc.replay.hedge_volatility_v0",
            "--feed-db",
            str(feed_db),
            "--runs-dir",
            str(args.runs_dir),
            "--run-id",
            run_id,
            "--json",
        ]
    else:
        cmd = [
            sys.executable,
            "-m",
            "kalshibtc.replay.cli",
            "--feed-db",
            str(feed_db),
            "--runs-dir",
            str(args.runs_dir),
            "--strategy",
            strategy,
            "--venue",
            venue,
            "--run-id",
            run_id,
            "--base-size-dollars",
            str(args.base_size_dollars),
            "--max-position-dollars",
            str(args.max_position_dollars),
            "--max-open-positions",
            str(args.max_open_positions),
            "--max-spread",
            str(args.max_spread),
            "--starting-bankroll",
            str(args.starting_bankroll),
            "--max-capital-at-risk",
            str(args.max_capital_at_risk),
            "--per-market-max-exposure",
            str(args.per_market_max_exposure),
            "--fee-per-contract",
            str(args.fee_per_contract),
            "--fee-rate",
            str(args.fee_rate),
            "--fill-timing",
            "next-tick",
            "--json",
        ]
    if window.get("from"):
        cmd += ["--from", str(window["from"])]
    if window.get("to"):
        cmd += ["--to", str(window["to"])]
    if venue == "polymarket" and strategy != "hedge_volatility_v0":
        cmd.append("--settlements-from-feed-db")
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=900, preexec_fn=child_preexec(args.child_nice))
    run_dir = (args.runs_dir / "replay" / strategy / run_id) if strategy == "hedge_volatility_v0" else (args.runs_dir / strategy / run_id)
    base = {
        "venue": venue,
        "strategy": strategy,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "feed_db": str(feed_db),
        "window": window,
        "allowed_venues": list(strategy_metadata(strategy).allowed_venues),
        "polymarket_only": strategy_metadata(strategy).polymarket_only,
    }
    if proc.returncode != 0:
        return {
            **base,
            "error": proc.stderr.strip() or proc.stdout.strip() or f"replay exited {proc.returncode}",
            "returncode": proc.returncode,
        }
    metrics_path = run_dir / "metrics.json"
    settlement_path = run_dir / "portfolio_settlement.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.exists() else {}
    settlement = json.loads(settlement_path.read_text(encoding="utf-8")) if settlement_path.exists() else {}
    pnl_split = settlement.get("pnl_split", {}) if isinstance(settlement, dict) else {}
    realized = float(
        settlement.get("realized_pnl", settlement.get("total_realized_pnl", metrics.get("realized_pnl", metrics.get("total_realized_pnl", 0.0))))
        or 0.0
    )
    notional = float(metrics.get("notional", metrics.get("total_notional", metrics.get("total_cost", 0.0))) or 0.0)
    return {
        **base,
        "snapshots": int(metrics.get("snapshots", metrics.get("snapshots_processed", 0)) or 0),
        "signals": int(metrics.get("signals", 0) or 0),
        "fills": int(metrics.get("fills", 0) or 0),
        "notional": round(notional, 6),
        "realized_pnl": round(realized, 6),
        "roi": round(realized / notional, 6) if notional else 0.0,
        "completed_pair_pnl": round(float(pnl_split.get("completed_pair_pnl", settlement.get("completed_pair_pnl", 0.0)) or 0.0), 6),
        "unpaired_pnl": round(float(pnl_split.get("unpaired_leftover_pnl", pnl_split.get("unpaired_pnl", settlement.get("unpaired_leftover_pnl", 0.0))) or 0.0), 6),
        "max_drawdown": round(float(metrics.get("max_drawdown", 0.0) or 0.0), 6),
        "win_rate": round(float(metrics.get("win_rate", 0.0) or 0.0), 6),
        "profit_factor": clean_profit_factor(metrics.get("profit_factor")),
        "max_capital_used": round(float(metrics.get("max_capital_used", 0.0) or 0.0), 6),
        "settlement_source": "kalshi_api" if venue == "kalshi" else "replay_final_snapshot",
    }


def selected_window(db: Path, *, from_ts: str | None, to_ts: str | None, row_limit: int | None) -> dict[str, Any]:
    sql = """
    SELECT ts, market_ticker
    FROM realtime_snapshots_1s
    WHERE COALESCE(strike, target_price, 0) > 0
    """
    params: list[Any] = []
    if from_ts:
        sql += " AND ts >= ?"
        params.append(from_ts)
    if to_ts:
        sql += " AND ts <= ?"
        params.append(to_ts)
    sql += " ORDER BY ts ASC, market_ticker ASC"
    if row_limit:
        sql += " LIMIT ?"
        params.append(row_limit)
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        rows = conn.execute(sql, params).fetchall()
    if not rows:
        raise ValueError(f"no replay rows selected for {db}")
    return {
        "from": rows[0][0],
        "to": rows[-1][0],
        "rows": len(rows),
        "markets": len({r[1] for r in rows}),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "venue",
        "strategy",
        "polymarket_only",
        "realized_pnl",
        "roi",
        "notional",
        "completed_pair_pnl",
        "unpaired_pnl",
        "risk_adjusted_score",
        "max_drawdown",
        "max_capital_used",
        "fills",
        "signals",
        "snapshots",
        "win_rate",
        "profit_factor",
        "run_dir",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in sorted(rows, key=lambda r: (r.get("venue", ""), -float(r.get("realized_pnl", 0.0)))):
            writer.writerow({k: row.get(k) for k in fields})


def write_md(path: Path, artifact: dict[str, Any]) -> None:
    lines = [
        "# Strategy × Venue Replay Matrix",
        "",
        f"Created: {artifact['created_at']}",
        f"Safety boundary: {artifact['safety_boundary']}",
        f"Scope: {artifact['scope']}",
        f"Workers: {artifact['worker_controls']['workers']} (reserve_cpus={artifact['worker_controls']['reserve_cpus']}, cpu_count={artifact['worker_controls']['cpu_count']}, child_nice={artifact['worker_controls']['child_nice']})",
        f"Eligible runs: {artifact['eligible_run_count']}; valid: {artifact['valid_run_count']}; errors: {artifact['error_count']}; skipped venue-ineligible: {artifact['skipped_count']}",
        "",
        "## Venue windows",
    ]
    for venue, window in artifact["venue_windows"].items():
        lines.append(
            f"- {venue}: {window['from']} → {window['to']} ({window['rows']} rows, {window['markets']} markets)"
        )
    lines += ["", "## Top by raw PnL"]
    for i, row in enumerate(artifact["top_by_raw_pnl"][:15], 1):
        lines.append(
            f"{i}. {row['venue']} / `{row['strategy']}`: pnl=${row['realized_pnl']:.2f}, roi={row['roi']*100:.2f}%, fills={row['fills']}, dd=${row.get('max_drawdown',0):.2f}"
        )
    lines += ["", "## Best by venue"]
    for venue, rows in artifact["best_by_venue"].items():
        lines.append(f"### {venue}")
        for row in rows[:8]:
            lines.append(
                f"- `{row['strategy']}`: pnl=${row['realized_pnl']:.2f}, roi={row['roi']*100:.2f}%, fills={row['fills']}, run={row['run_id']}"
            )
    if artifact["skipped"]:
        lines += ["", "## Skipped venue-ineligible"]
        for row in artifact["skipped"]:
            lines.append(f"- {row['venue']} / `{row['strategy']}` skipped; allowed={row['allowed_venues']}")
    if artifact["errors"]:
        lines += ["", "## Errors"]
        for row in artifact["errors"]:
            lines.append(f"- {row['venue']} / `{row['strategy']}`: {row['error']}")
    lines += [
        "",
        "## Caution",
        "- This is a same-window replay matrix, not parameter optimization.",
        "- Kalshi uses official settlements when available; Polymarket settlement is labeled replay-final-snapshot proxy unless separately reconciled.",
        "- No live/paper order submission occurred.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def clean_profit_factor(value: Any) -> float | str:
    try:
        val = float(value)
    except Exception:
        return ""
    return round(val, 6) if math.isfinite(val) else str(value)


if __name__ == "__main__":
    raise SystemExit(main())
