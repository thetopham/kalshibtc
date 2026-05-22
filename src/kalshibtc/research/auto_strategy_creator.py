from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .journal import build_research_journal
from .specs import CandidateSpec


@dataclass(frozen=True)
class HypothesisTemplate:
    name: str
    economic_story: str
    mechanism: str
    replay_strategy: str
    parameters: dict[str, Any]
    falsification: str


@dataclass(frozen=True)
class CreatedStrategyRun:
    spec: CandidateSpec
    strategy: str
    run_id: str
    run_dir: Path
    metrics_path: Path
    report_path: Path
    journal_latest_path: Path | None
    exit_code: int


TEMPLATES: tuple[HypothesisTemplate, ...] = (
    HypothesisTemplate(
        name="brownian_edge_high_confidence",
        economic_story=(
            "Executable YES/NO prices sometimes lag a simple distance-to-strike probability; "
            "only trade when the edge is unusually large and the probability is away from 50/50."
        ),
        mechanism="Brownian probability model with stricter edge, mid-band, and volatility gates.",
        replay_strategy="strategy_probability_mm_v0",
        parameters={
            "probability_model": "brownian",
            "edge_threshold": 0.06,
            "min_abs_edge": 0.04,
            "max_probability_mid_band": 0.08,
            "base_notional": 10.0,
            "min_seconds_to_close": 75.0,
            "max_wickiness": 0.7,
            "max_atr_slope": 1.2,
            "min_volatility": 1.0,
        },
        falsification="Reject if after-fee realized PnL is negative, fills are sparse, or max drawdown exceeds realized PnL on validation/test replay.",
    ),
    HypothesisTemplate(
        name="late_brownian_edge_only",
        economic_story=(
            "Nearer expiry, distance-to-strike should dominate noisy microstructure; require a larger edge "
            "but allow a later window before the final forced no-trade zone."
        ),
        mechanism="Brownian probability model with later min_seconds gate and larger edge threshold.",
        replay_strategy="strategy_probability_mm_v0",
        parameters={
            "probability_model": "brownian",
            "edge_threshold": 0.08,
            "min_abs_edge": 0.06,
            "max_probability_mid_band": 0.12,
            "base_notional": 10.0,
            "min_seconds_to_close": 45.0,
            "max_wickiness": 0.8,
            "max_atr_slope": 1.5,
            "min_volatility": 1.0,
        },
        falsification="Reject if late-window fills increase drawdown or lose to fees/spread despite larger nominal edge.",
    ),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kbtc-auto-strategy-creator",
        description="Research-only loop: choose a simple hypothesis, replay it, and publish artifacts/journal summaries.",
    )
    parser.add_argument("--feed-db", default="feed/kalshi-btc-1s.sqlite3")
    parser.add_argument("--runs-dir", default="runs")
    parser.add_argument("--research-dir", default="research")
    parser.add_argument("--venue", choices=("kalshi",), default="kalshi")
    parser.add_argument("--from", dest="from_ts", default=None)
    parser.add_argument("--to", dest="to_ts", default=None)
    parser.add_argument("--hypothesis", choices=[template.name for template in TEMPLATES], default=None)
    parser.add_argument("--seed", default="default", help="Deterministic seed for hypothesis choice when --hypothesis is omitted.")
    parser.add_argument("--run-id-prefix", default="auto")
    parser.add_argument("--base-size-dollars", type=float, default=25.0)
    parser.add_argument("--max-position-dollars", type=float, default=25.0)
    parser.add_argument("--max-open-positions", type=int, default=1)
    parser.add_argument("--max-spread", type=float, default=0.05)
    parser.add_argument("--fee-rate", type=float, default=0.0)
    parser.add_argument("--fill-timing", choices=("next-tick", "same-tick"), default="next-tick")
    parser.add_argument("--settlements-from-feed-db", action="store_true", default=True)
    parser.add_argument("--dry-run", action="store_true", help="Write the candidate spec/report but do not invoke replay.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    runs_dir = Path(args.runs_dir)
    research_dir = Path(args.research_dir)
    template = _choose_template(args.hypothesis, seed=args.seed)
    created_at = datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    parent_id = _parent_id(template=template, seed=args.seed, from_ts=args.from_ts, to_ts=args.to_ts)
    spec = CandidateSpec.from_mapping(
        {
            "name": template.name,
            "economic_story": template.economic_story,
            "mechanism": template.mechanism,
            "parameters": {
                "replay_strategy": template.replay_strategy,
                "venue": args.venue,
                "feed_db": args.feed_db,
                "from_ts": args.from_ts,
                "to_ts": args.to_ts,
                "strategy_params": template.parameters,
                "risk": {
                    "base_size_dollars": args.base_size_dollars,
                    "max_position_dollars": args.max_position_dollars,
                    "max_open_positions": args.max_open_positions,
                    "max_spread": args.max_spread,
                    "fee_rate": args.fee_rate,
                    "fill_timing": args.fill_timing,
                },
            },
            "falsification": template.falsification,
            "created_at": created_at,
            "parent_id": parent_id,
        }
    )
    run_id = f"{args.run_id_prefix}-{template.name}-{created_at.replace(':', '').replace('-', '')}"
    report_path = research_dir / "auto_strategy_creator" / f"{run_id}.md"
    spec_path = research_dir / "auto_strategy_creator" / f"{run_id}.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    replay_exit = 0
    metrics_path = runs_dir / template.replay_strategy / run_id / "metrics.json"
    if not args.dry_run:
        cmd = _replay_command(args=args, template=template, run_id=run_id)
        completed = subprocess.run(cmd, cwd=Path.cwd(), text=True, capture_output=True, check=False)
        replay_exit = completed.returncode
        if completed.stdout:
            (spec_path.parent / f"{run_id}.stdout.log").write_text(completed.stdout, encoding="utf-8")
        if completed.stderr:
            (spec_path.parent / f"{run_id}.stderr.log").write_text(completed.stderr, encoding="utf-8")
        if replay_exit != 0:
            report_path.write_text(
                _render_report(spec=spec, template=template, run_id=run_id, metrics=None, metrics_path=metrics_path, error=completed.stderr.strip() or completed.stdout.strip()),
                encoding="utf-8",
            )
            print(f"auto_strategy_creator replay_failed run_id={run_id} report={report_path}", file=sys.stderr)
            return replay_exit

    metrics = _read_json(metrics_path) if metrics_path.exists() else None
    journal_result = build_research_journal(runs_dir=runs_dir, out_dir=research_dir)
    report_path.write_text(
        _render_report(spec=spec, template=template, run_id=run_id, metrics=metrics, metrics_path=metrics_path, error=None),
        encoding="utf-8",
    )
    _append_manifest(
        research_dir / "auto_strategy_creator" / "manifest.csv",
        spec=spec,
        template=template,
        run_id=run_id,
        metrics=metrics,
        report_path=report_path,
        metrics_path=metrics_path,
    )

    result = {
        "run_id": run_id,
        "hypothesis": template.name,
        "strategy": template.replay_strategy,
        "spec_path": str(spec_path),
        "metrics_path": str(metrics_path),
        "report_path": str(report_path),
        "journal_latest_path": str(journal_result.latest_report_path),
        "fills": int(metrics.get("fills", 0)) if isinstance(metrics, dict) else None,
        "realized_pnl": _realized_pnl(metrics),
        "safety_boundary": "replay/research only; no live orders",
    }
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(
            "auto_strategy_creator "
            f"hypothesis={template.name} strategy={template.replay_strategy} "
            f"run_id={run_id} report={report_path} latest={journal_result.latest_report_path}"
        )
    return 0


def _choose_template(name: str | None, *, seed: str) -> HypothesisTemplate:
    if name:
        return next(template for template in TEMPLATES if template.name == name)
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return TEMPLATES[int.from_bytes(digest[:4], "big") % len(TEMPLATES)]


def _parent_id(*, template: HypothesisTemplate, seed: str, from_ts: str | None, to_ts: str | None) -> str:
    payload = json.dumps({"template": template.name, "seed": seed, "from_ts": from_ts, "to_ts": to_ts}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _replay_command(*, args: argparse.Namespace, template: HypothesisTemplate, run_id: str) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "kalshibtc.replay.cli",
        "--feed-db",
        args.feed_db,
        "--runs-dir",
        args.runs_dir,
        "--strategy",
        template.replay_strategy,
        "--venue",
        args.venue,
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
        "--fee-rate",
        str(args.fee_rate),
        "--fill-timing",
        args.fill_timing,
        "--json",
    ]
    if args.from_ts:
        cmd.extend(["--from", args.from_ts])
    if args.to_ts:
        cmd.extend(["--to", args.to_ts])
    if args.settlements_from_feed_db:
        cmd.append("--settlements-from-feed-db")
    for key, value in template.parameters.items():
        cmd.extend(["--strategy-param", f"{key}={json.dumps(value)}"])
    return cmd


def _render_report(
    *,
    spec: CandidateSpec,
    template: HypothesisTemplate,
    run_id: str,
    metrics: dict[str, Any] | None,
    metrics_path: Path,
    error: str | None,
) -> str:
    lines = [
        f"# Auto Strategy Creator: {template.name}",
        "",
        "Safety boundary: replay/research only; no live orders.",
        "",
        f"Run ID: `{run_id}`",
        f"Replay strategy: `{template.replay_strategy}`",
        f"Metrics: `{metrics_path}`",
        "",
        "## Hypothesis",
        "",
        spec.economic_story,
        "",
        f"Mechanism: {spec.mechanism}",
        "",
        f"Falsification rule: {spec.falsification}",
        "",
        "## Parameters",
        "",
        "```json",
        json.dumps(spec.parameters, indent=2, sort_keys=True),
        "```",
        "",
    ]
    if error:
        lines += ["## Replay error", "", "```", error, "```", ""]
        return "\n".join(lines)
    if not metrics:
        lines += ["## Result", "", "Dry run only; replay was not executed.", ""]
        return "\n".join(lines)
    raw_portfolio = metrics.get("portfolio_settlement")
    raw_institutional = metrics.get("institutional_metrics")
    portfolio: dict[str, Any] = raw_portfolio if isinstance(raw_portfolio, dict) else {}
    institutional: dict[str, Any] = raw_institutional if isinstance(raw_institutional, dict) else {}
    lines += [
        "## Result",
        "",
        f"Snapshots: {int(metrics.get('snapshots') or 0):,}",
        f"Signals: {int(metrics.get('signals') or 0):,}",
        f"Fills: {int(metrics.get('fills') or 0):,}",
        f"Notional: ${float(metrics.get('notional') or 0.0):,.2f}",
        f"Realized PnL: ${float(portfolio.get('realized_pnl') or institutional.get('total_pnl') or 0.0):,.2f}",
        f"Profit factor: {float(institutional.get('profit_factor') or 0.0):.4f}",
        f"Max drawdown: ${float(institutional.get('max_drawdown') or 0.0):,.2f}",
        f"Settlement source: {institutional.get('settlement_source') or 'unknown'}",
        "",
    ]
    return "\n".join(lines)


def _append_manifest(
    path: Path,
    *,
    spec: CandidateSpec,
    template: HypothesisTemplate,
    run_id: str,
    metrics: dict[str, Any] | None,
    report_path: Path,
    metrics_path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["created_at", "hypothesis", "strategy", "run_id", "fills", "realized_pnl", "metrics_path", "report_path"],
        )
        if not exists:
            writer.writeheader()
        writer.writerow(
            {
                "created_at": spec.created_at,
                "hypothesis": template.name,
                "strategy": template.replay_strategy,
                "run_id": run_id,
                "fills": int(metrics.get("fills", 0)) if isinstance(metrics, dict) else "",
                "realized_pnl": _realized_pnl(metrics) if isinstance(metrics, dict) else "",
                "metrics_path": str(metrics_path),
                "report_path": str(report_path),
            }
        )


def _read_json(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def _realized_pnl(metrics: dict[str, Any] | None) -> float | None:
    if not isinstance(metrics, dict):
        return None
    raw_portfolio = metrics.get("portfolio_settlement")
    raw_institutional = metrics.get("institutional_metrics")
    portfolio: dict[str, Any] = raw_portfolio if isinstance(raw_portfolio, dict) else {}
    institutional: dict[str, Any] = raw_institutional if isinstance(raw_institutional, dict) else {}
    return float(portfolio.get("realized_pnl") or institutional.get("total_pnl") or 0.0)


if __name__ == "__main__":
    raise SystemExit(main())
