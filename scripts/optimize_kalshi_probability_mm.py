#!/usr/bin/env python3
"""Bounded chronological optimization for Kalshi strategy_probability_mm_v0.

Read-only: consumes feed/kalshi-btc-1s.sqlite3, uses official Kalshi settlements
when available, and writes summary artifacts under runs/strategy_comparisons/.

Metric discipline: optimize on validation risk-adjusted score after screening out
thin/no-trade candidates and high-leakage pair artifacts; report test once.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import itertools
import json
import math
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FEED_DB = ROOT / "feed" / "kalshi-btc-1s.sqlite3"
RUNS_DIR = ROOT / "runs"
STRATEGY = "strategy_probability_mm_v0"

# Bounded grid around the only Kalshi default strategy that had positive full-feed PnL.
GRID: dict[str, list[Any]] = {
    "edge_threshold": [0.02, 0.03, 0.04, 0.05, 0.08],
    "base_notional": [5.0, 10.0, 15.0],
    "max_net_ratio": [0.1, 0.25, 0.5],
    "force_flatten_seconds": [30.0, 60.0, 120.0],
    "min_seconds_to_close": [30.0, 60.0, 120.0],
    "max_wickiness": [0.6, 0.75, 0.9],
    "max_atr_slope": [0.75, 1.5, 3.0],
    "min_volatility": [0.5, 1.0, 2.0],
}

DEFAULT_PARAMS = {
    "edge_threshold": 0.03,
    "base_notional": 10.0,
    "max_net_ratio": 0.25,
    "force_flatten_seconds": 60.0,
    "min_seconds_to_close": 30.0,
    "max_wickiness": 0.75,
    "max_atr_slope": 1.5,
    "min_volatility": 1.0,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Optimize Kalshi probability-MM replay parameters.")
    parser.add_argument("--feed-db", type=Path, default=FEED_DB)
    parser.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    parser.add_argument("--max-candidates", type=int, default=80)
    parser.add_argument("--top-validation", type=int, default=8)
    parser.add_argument("--workers", type=int, default=None, help="Parallel candidate workers. Default reserves --reserve-cpus cores for live 1s feeds and system services.")
    parser.add_argument("--reserve-cpus", type=int, default=4, help="CPU cores to leave unused by replay workers; protects live datafeed recorders.")
    parser.add_argument("--child-nice", type=int, default=10, help="Nice value applied to child replay processes on POSIX so datafeeds stay responsive.")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    started = datetime.now(tz=UTC)
    markets = settled_markets(args.feed_db)
    if len(markets) < 20:
        raise SystemExit(f"not enough settled markets for split optimization: {len(markets)}")
    splits = chronological_splits(markets)
    candidates = build_candidates(args.max_candidates)
    workers = effective_workers(args.workers, reserve_cpus=args.reserve_cpus)
    run_prefix = f"kalshi-prob-mm-opt-{started.strftime('%Y%m%dT%H%M%SZ')}"
    out_stem = args.out or args.runs_dir / "strategy_comparisons" / run_prefix
    out_stem.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path = out_stem.with_suffix(".checkpoint.json")
    checkpoint_csv_path = out_stem.with_suffix(".checkpoint.csv")

    print(
        f"kalshi probability-mm optimization start markets={len(markets)} candidates={len(candidates)} train={len(splits['train'])} validation={len(splits['validation'])} test={len(splits['test'])} workers={workers} reserve_cpus={args.reserve_cpus} child_nice={args.child_nice}",
        flush=True,
    )
    train_rows = []
    validation_rows = []
    test_rows = []

    candidate_jobs = list(enumerate(candidates, start=1))
    completed = 0
    if workers <= 1:
        for idx, params in candidate_jobs:
            train, validation = run_candidate(args, run_prefix, idx, splits, params)
            train_rows.append(train)
            validation_rows.append(validation)
            completed += 1
            print_candidate_progress(completed, len(candidates), train, validation)
            write_checkpoint(checkpoint_path, checkpoint_csv_path, started, args, run_prefix, markets, splits, candidates, train_rows, validation_rows, test_rows, status=f"candidate_{idx}_complete")
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_idx = {executor.submit(run_candidate, args, run_prefix, idx, splits, params): idx for idx, params in candidate_jobs}
            for future in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[future]
                train, validation = future.result()
                train_rows.append(train)
                validation_rows.append(validation)
                completed += 1
                print_candidate_progress(completed, len(candidates), train, validation)
                write_checkpoint(checkpoint_path, checkpoint_csv_path, started, args, run_prefix, markets, splits, candidates, train_rows, validation_rows, test_rows, status=f"candidate_{idx}_complete")
    train_rows.sort(key=lambda r: r.get("run_id", ""))
    validation_rows.sort(key=lambda r: r.get("run_id", ""))

    finalists = sorted(
        [r for r in validation_rows if not r.get("error") and not r.get("skipped_reason") and passes_validation_gate(r)],
        key=lambda r: r["selection_score"],
        reverse=True,
    )[: args.top_validation]
    for rank, validation in enumerate(finalists, start=1):
        params = validation["params"]
        test = run_replay(args, run_prefix, rank, "test", splits["test"], params)
        test["validation_selection_score"] = validation["selection_score"]
        test_rows.append(test)
        write_checkpoint(checkpoint_path, checkpoint_csv_path, started, args, run_prefix, markets, splits, candidates, train_rows, validation_rows, test_rows, status=f"test_finalist_{rank}_complete")

    finished = datetime.now(tz=UTC)
    artifact = {
        "created_at": finished.isoformat(),
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": (finished - started).total_seconds(),
        "scope": "Kalshi-only bounded optimization for strategy_probability_mm_v0 using official settled markets; train is a chronological row-strided screening subset, validation/test are contiguous chronological holdouts.",
        "safety_boundary": "read-only replay/backtest only; no order submission",
        "feed_db": str(args.feed_db),
        "strategy": STRATEGY,
        "market_count": len(markets),
        "splits": {name: split_info(ms) for name, ms in splits.items()},
        "candidate_count": len(candidates),
        "worker_controls": {"workers": workers, "reserve_cpus": args.reserve_cpus, "cpu_count": os.cpu_count(), "child_nice": args.child_nice},
        "metrics_to_optimize": metrics_manifest(),
        "selection_rule": "Pick by validation selection_score after training/validation gates; evaluate selected finalists on test once.",
        "top_validation": sorted([r for r in validation_rows if not r.get("error") and not r.get("skipped_reason")], key=lambda r: r.get("selection_score", -10**9), reverse=True)[:20],
        "test_results": sorted(test_rows, key=lambda r: r.get("selection_score", -10**9), reverse=True),
        "train_results": train_rows,
        "validation_results": validation_rows,
        "errors": [r for r in itertools.chain(train_rows, validation_rows, test_rows) if r.get("error")],
    }
    json_path = out_stem.with_suffix(".json")
    csv_path = out_stem.with_suffix(".csv")
    md_path = out_stem.with_suffix(".md")
    json_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(csv_path, train_rows, validation_rows, test_rows)
    write_md(md_path, artifact)
    payload = {
        "json": str(json_path),
        "csv": str(csv_path),
        "md": str(md_path),
        "candidates": len(candidates),
        "finalists": len(test_rows),
        "best_validation": artifact["top_validation"][:3],
        "test_results": artifact["test_results"],
    }
    print(json.dumps(payload, sort_keys=True) if args.json else payload)
    return 0 if not artifact["errors"] else 1


def settled_markets(feed_db: Path) -> list[dict[str, str]]:
    with sqlite3.connect(f"file:{feed_db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT s.market_ticker, MIN(s.ts) AS first_ts, MAX(s.ts) AS last_ts, MAX(s.market_close_time) AS close_ts
                FROM realtime_snapshots_1s s
                JOIN market_settlements m ON s.market_ticker = m.market_ticker
                WHERE m.source = 'kalshi_api'
                  AND m.status = 'settled_official'
                  AND m.winning_side IN ('yes', 'no')
                  AND COALESCE(s.strike, s.target_price, 0) > 0
                GROUP BY s.market_ticker
                ORDER BY close_ts ASC, first_ts ASC
                """
            )
        ]


def chronological_splits(markets: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    n = len(markets)
    train_end = max(1, int(n * 0.70))
    validation_end = max(train_end + 1, int(n * 0.85))
    return {
        "train": markets[:train_end:6],
        "validation": markets[train_end:validation_end],
        "test": markets[validation_end:],
    }


def build_candidates(max_candidates: int) -> list[dict[str, Any]]:
    configs: list[dict[str, Any]] = [dict(DEFAULT_PARAMS)]
    for key, values in GRID.items():
        for value in values:
            cfg = dict(DEFAULT_PARAMS)
            cfg[key] = value
            configs.append(cfg)
    # Add a deterministic, bounded diagonal sample of interactions without a huge Cartesian explosion.
    keys = list(GRID)
    max_len = max(len(v) for v in GRID.values())
    for offset in range(max_len * 6):
        cfg = dict(DEFAULT_PARAMS)
        for i, key in enumerate(keys):
            values = GRID[key]
            cfg[key] = values[(offset + i) % len(values)]
        configs.append(cfg)
    seen = set()
    unique = []
    for cfg in configs:
        sig = json.dumps(cfg, sort_keys=True)
        if sig not in seen:
            seen.add(sig)
            unique.append(cfg)
        if len(unique) >= max_candidates:
            break
    return unique


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


def run_candidate(args: argparse.Namespace, run_prefix: str, idx: int, splits: dict[str, list[dict[str, str]]], params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    label = param_label(params)
    train = run_replay(args, run_prefix, idx, "train", splits["train"], params)
    if not passes_training_gate(train):
        validation = {**stub_row(params, label), "split": "validation", "skipped_reason": "failed_training_gate"}
    else:
        validation = run_replay(args, run_prefix, idx, "validation", splits["validation"], params)
    return train, validation


def print_candidate_progress(done: int, total: int, train: dict[str, Any], validation: dict[str, Any]) -> None:
    print(
        f"candidate {done}/{total} run={train.get('run_id')} train_pnl={train.get('realized_pnl')} train_score={train.get('selection_score')} validation_pnl={validation.get('realized_pnl')} validation_score={validation.get('selection_score')} gate={train.get('passes_training_gate')}",
        flush=True,
    )


def run_replay(args: argparse.Namespace, run_prefix: str, idx: int, split: str, markets: list[dict[str, str]], params: dict[str, Any]) -> dict[str, Any]:
    from_ts = markets[0]["first_ts"]
    to_ts = markets[-1]["last_ts"]
    run_id = f"{run_prefix}-{split}-{idx:03d}"
    cmd = [
        sys.executable,
        "-m",
        "kalshibtc.replay.cli",
        "--feed-db",
        str(args.feed_db),
        "--runs-dir",
        str(args.runs_dir),
        "--strategy",
        STRATEGY,
        "--venue",
        "kalshi",
        "--run-id",
        run_id,
        "--from",
        from_ts,
        "--to",
        to_ts,
        "--base-size-dollars",
        "25",
        "--max-position-dollars",
        "5000",
        "--max-open-positions",
        "100000",
        "--max-spread",
        "1.0",
        "--starting-bankroll",
        "100000",
        "--max-capital-at-risk",
        "100000",
        "--per-market-max-exposure",
        "5000",
        "--fill-timing",
        "next-tick",
        "--json",
    ]
    for key, value in params.items():
        cmd += ["--strategy-param", f"{key}={value}"]
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=900, preexec_fn=child_preexec(args.child_nice))
    label = param_label(params)
    run_dir = args.runs_dir / STRATEGY / run_id
    base = {
        "split": split,
        "strategy": STRATEGY,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "from": from_ts,
        "to": to_ts,
        "market_count": len(markets),
        "params": params,
        "param_label": label,
    }
    if proc.returncode != 0:
        return {**base, "error": proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"}
    metrics_path = run_dir / "metrics.json"
    settlement_path = run_dir / "portfolio_settlement.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    settlement = json.loads(settlement_path.read_text(encoding="utf-8")) if settlement_path.exists() else {}
    split_payload = settlement.get("pnl_split", {})
    realized = float(settlement.get("realized_pnl", 0.0) or 0.0)
    completed_pair = float(split_payload.get("completed_pair_pnl", 0.0) or 0.0)
    unpaired = float(split_payload.get("unpaired_leftover_pnl", 0.0) or 0.0)
    notional = float(metrics.get("notional", 0.0) or 0.0)
    fills = int(metrics.get("fills", 0) or 0)
    traded_markets = count_traded_markets(run_dir / "results.sqlite3")
    markets_with_loss, worst_market_pnl = market_loss_stats(run_dir / "results.sqlite3")
    row = {
        **base,
        "snapshots": int(metrics.get("snapshots", 0) or 0),
        "signals": int(metrics.get("signals", 0) or 0),
        "fills": fills,
        "traded_markets": traded_markets,
        "notional": round(notional, 6),
        "realized_pnl": round(realized, 6),
        "roi": round(realized / notional, 6) if notional else 0.0,
        "pnl_per_market": round(realized / len(markets), 6) if markets else 0.0,
        "pnl_per_traded_market": round(realized / traded_markets, 6) if traded_markets else 0.0,
        "completed_pair_pnl": round(completed_pair, 6),
        "unpaired_pnl": round(unpaired, 6),
        "unpaired_leakage_ratio": round(abs(unpaired) / max(abs(completed_pair), 1.0), 6),
        "worst_market_pnl": round(worst_market_pnl, 6),
        "losing_traded_markets": markets_with_loss,
        "fill_density": round(fills / len(markets), 6) if markets else 0.0,
        "settlement_source": "kalshi_api",
    }
    row["selection_score"] = selection_score(row)
    row["passes_training_gate"] = passes_training_gate(row)
    row["passes_validation_gate"] = passes_validation_gate(row)
    return row


def count_traded_markets(results_db: Path) -> int:
    if not results_db.exists():
        return 0
    with sqlite3.connect(f"file:{results_db}?mode=ro", uri=True) as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='portfolio_settlement_by_market'").fetchone()
        if exists:
            return int(conn.execute("SELECT COUNT(*) FROM portfolio_settlement_by_market WHERE total_cost > 0").fetchone()[0] or 0)
        return 0


def market_loss_stats(results_db: Path) -> tuple[int, float]:
    if not results_db.exists():
        return 0, 0.0
    with sqlite3.connect(f"file:{results_db}?mode=ro", uri=True) as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='portfolio_settlement_by_market'").fetchone()
        if not exists:
            return 0, 0.0
        rows = [float(r[0] or 0.0) for r in conn.execute("SELECT realized_pnl FROM portfolio_settlement_by_market")]
    return sum(1 for v in rows if v < 0), min(rows) if rows else 0.0


def selection_score(row: dict[str, Any]) -> float:
    pnl = float(row.get("realized_pnl", 0.0) or 0.0)
    worst = abs(min(float(row.get("worst_market_pnl", 0.0) or 0.0), 0.0))
    unpaired = abs(float(row.get("unpaired_pnl", 0.0) or 0.0))
    leakage = float(row.get("unpaired_leakage_ratio", 0.0) or 0.0)
    fill_density = float(row.get("fill_density", 0.0) or 0.0)
    # Reward official realized PnL, penalize single-contract tail risk, residual exposure, and hyperactive fill density.
    return round(pnl - 0.75 * worst - 0.15 * unpaired - 5.0 * max(0.0, leakage - 1.0) - 0.02 * max(0.0, fill_density - 5.0), 6)


def passes_training_gate(row: dict[str, Any]) -> bool:
    return int(row.get("fills", 0) or 0) >= 20 and int(row.get("traded_markets", 0) or 0) >= 5


def passes_validation_gate(row: dict[str, Any]) -> bool:
    return passes_training_gate(row) and float(row.get("realized_pnl", 0.0) or 0.0) > 0.0 and float(row.get("selection_score", -10**9)) > 0.0


def stub_row(params: dict[str, Any], label: str) -> dict[str, Any]:
    return {"strategy": STRATEGY, "params": params, "param_label": label}


def param_label(params: dict[str, Any]) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(params.items()))


def split_info(markets: list[dict[str, str]]) -> dict[str, Any]:
    return {"markets": len(markets), "from": markets[0]["first_ts"], "to": markets[-1]["last_ts"], "first_close": markets[0]["close_ts"], "last_close": markets[-1]["close_ts"]}


def metrics_manifest() -> list[dict[str, str]]:
    return [
        {"metric": "official realized PnL", "role": "primary reward; uses Kalshi official settlement rows"},
        {"metric": "worst-market PnL", "role": "tail-risk penalty; prevents one catastrophic 15m contract"},
        {"metric": "unpaired/residual PnL and leakage ratio", "role": "penalizes directional leftovers masquerading as pair edge"},
        {"metric": "traded markets + fills", "role": "minimum sample gate; avoids no-trade winners"},
        {"metric": "Pnl per market / ROI", "role": "secondary diagnostics, not primary optimizer alone"},
    ]



def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_checkpoint(
    json_path: Path,
    csv_path: Path,
    started: datetime,
    args: argparse.Namespace,
    run_prefix: str,
    markets: list[dict[str, str]],
    splits: dict[str, list[dict[str, str]]],
    candidates: list[dict[str, Any]],
    train_rows: list[dict[str, Any]],
    validation_rows: list[dict[str, Any]],
    test_rows: list[dict[str, Any]],
    *,
    status: str,
) -> None:
    now = datetime.now(tz=UTC)
    artifact = {
        "checkpoint": True,
        "status": status,
        "created_at": now.isoformat(),
        "started_at": started.isoformat(),
        "duration_seconds": (now - started).total_seconds(),
        "safety_boundary": "read-only replay/backtest only; no order submission",
        "feed_db": str(args.feed_db),
        "strategy": STRATEGY,
        "run_prefix": run_prefix,
        "market_count": len(markets),
        "splits": {name: split_info(ms) for name, ms in splits.items()},
        "candidate_count": len(candidates),
        "worker_controls": {"workers": effective_workers(args.workers, reserve_cpus=args.reserve_cpus), "reserve_cpus": args.reserve_cpus, "cpu_count": os.cpu_count(), "child_nice": args.child_nice},
        "completed_train": len(train_rows),
        "completed_validation": len(validation_rows),
        "completed_test": len(test_rows),
        "top_validation_so_far": sorted(
            [r for r in validation_rows if not r.get("error") and not r.get("skipped_reason")],
            key=lambda r: r.get("selection_score", -10**9),
            reverse=True,
        )[:20],
        "test_results_so_far": sorted(test_rows, key=lambda r: r.get("selection_score", -10**9), reverse=True),
        "train_results": train_rows,
        "validation_results": validation_rows,
        "test_results": test_rows,
        "errors": [r for r in itertools.chain(train_rows, validation_rows, test_rows) if r.get("error")],
    }
    atomic_write_text(json_path, json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    tmp_csv = csv_path.with_suffix(csv_path.suffix + ".tmp")
    write_csv(tmp_csv, train_rows, validation_rows, test_rows)
    os.replace(tmp_csv, csv_path)


def write_csv(path: Path, *groups: Iterable[dict[str, Any]]) -> None:
    fields = ["split", "param_label", "selection_score", "realized_pnl", "roi", "pnl_per_market", "pnl_per_traded_market", "completed_pair_pnl", "unpaired_pnl", "unpaired_leakage_ratio", "worst_market_pnl", "losing_traded_markets", "fills", "traded_markets", "notional", "run_dir", "skipped_reason", "error"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in itertools.chain.from_iterable(groups):
            w.writerow({k: row.get(k) for k in fields})


def write_md(path: Path, artifact: dict[str, Any]) -> None:
    lines = [
        "# Kalshi probability-MM bounded optimization",
        "",
        f"Created: {artifact['created_at']}",
        f"Safety boundary: {artifact['safety_boundary']}",
        f"Markets: {artifact['market_count']} official-settled Kalshi markets",
        f"Workers: {artifact['worker_controls']['workers']} (reserve_cpus={artifact['worker_controls']['reserve_cpus']}, cpu_count={artifact['worker_controls']['cpu_count']}, child_nice={artifact['worker_controls']['child_nice']})",
        "",
        "## Splits",
    ]
    for name, info in artifact["splits"].items():
        lines.append(f"- {name}: {info['markets']} markets, {info['from']} → {info['to']}")
    lines += ["", "## Optimized metrics"]
    for metric in artifact["metrics_to_optimize"]:
        lines.append(f"- {metric['metric']}: {metric['role']}")
    lines += ["", "## Top validation candidates"]
    for i, row in enumerate(artifact["top_validation"][:10], 1):
        lines.append(f"{i}. score={row['selection_score']:.2f}, pnl=${row['realized_pnl']:.2f}, worst=${row['worst_market_pnl']:.2f}, unpaired=${row['unpaired_pnl']:.2f}, fills={row['fills']}, params=`{row['param_label']}`")
    lines += ["", "## Test-once finalists"]
    for i, row in enumerate(artifact["test_results"], 1):
        lines.append(f"{i}. test_score={row['selection_score']:.2f}, test_pnl=${row['realized_pnl']:.2f}, roi={row['roi']*100:.2f}%, worst=${row['worst_market_pnl']:.2f}, unpaired=${row['unpaired_pnl']:.2f}, fills={row['fills']}, params=`{row['param_label']}`")
    lines += ["", "## Caution", "- This is bounded optimization over one strategy family, not proof of deployability.", "- Finalists need full realism audit: fees, depth/min-size, no-lookahead, and live-orderbook fill validation before paper/live consideration."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
