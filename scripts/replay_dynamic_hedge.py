from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from kalshibtc.runtime_paths import DEFAULT_FEED_DB, DEFAULT_RUNS_DIR, resolve_feed_db, resolve_runs_dir
from kalshibtc.strategy.dynamic_complement_hedge import DynamicHedgeConfig, replay_feed_db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay/scan Dynamic Complement Hedge Bot against the Kalshi BTC 1s feed DB. Paper/read-only only."
    )
    parser.add_argument("--feed-db", default=None, help=f"Feed SQLite DB. Default: {DEFAULT_FEED_DB}")
    parser.add_argument("--runs-dir", default=None, help=f"Runs root. Default: {DEFAULT_RUNS_DIR}")
    parser.add_argument("--run-id", default=None, help="Run id. Defaults to current UTC timestamp.")
    parser.add_argument("--from", dest="from_ts", default=None)
    parser.add_argument("--to", dest="to_ts", default=None)
    parser.add_argument("--scan-only", action="store_true", help="Do not trade; only scan temporal basis compression after initial entry.")
    parser.add_argument("--initial-shares", type=float, default=100.0)
    parser.add_argument("--hedge-shares", type=float, default=25.0)
    parser.add_argument("--max-total-shares", type=float, default=200.0)
    parser.add_argument("--max-total-cost", type=float, default=200.0)
    parser.add_argument("--required-edge", type=float, default=0.03)
    parser.add_argument("--slippage", type=float, default=0.01)
    parser.add_argument("--fee-per-share", type=float, default=0.0)
    parser.add_argument("--cooldown-seconds", type=float, default=3.0)
    parser.add_argument("--edge-persistence-snapshots", type=int, default=2)
    parser.add_argument("--allow-overhedge", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    feed_db = resolve_feed_db(args.feed_db)
    runs_dir = resolve_runs_dir(args.runs_dir)
    run_id = args.run_id or datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    mode = "temporal_scan" if args.scan_only else "paper_trade"
    out_dir = Path(runs_dir) / "dynamic_complement_hedge" / f"{mode}-{run_id}"
    config = DynamicHedgeConfig(
        initial_shares=args.initial_shares,
        hedge_shares=args.hedge_shares,
        max_total_shares=args.max_total_shares,
        max_total_cost=args.max_total_cost,
        required_edge=args.required_edge,
        slippage=args.slippage,
        fee_per_share=args.fee_per_share,
        cooldown_seconds=args.cooldown_seconds,
        edge_persistence_snapshots=args.edge_persistence_snapshots,
        allow_overhedge=args.allow_overhedge,
    )
    summary = replay_feed_db(
        feed_db,
        out_dir=out_dir,
        config=config,
        scan_only=args.scan_only,
        from_ts=args.from_ts,
        to_ts=args.to_ts,
    )
    summary["run_dir"] = str(out_dir)
    if args.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        compression = summary["temporal_basis_compression"]
        print(
            "dynamic_complement_hedge "
            f"mode={summary['mode']} markets={summary['markets_tested']} "
            f"traded={summary['markets_traded']} hedged={summary['markets_hedged']} "
            f"pnl={summary['total_paper_pnl']} fills={summary['fills']} "
            f"best_lt_95={compression['count_best_combined_basis_lt']['0.95']} "
            f"run_dir={out_dir}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
