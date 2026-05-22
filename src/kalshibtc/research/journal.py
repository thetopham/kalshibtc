from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CSV_FIELDS = [
    "run_date",
    "strategy",
    "realized_pnl",
    "run_timestamp",
    "run_id",
    "exchange",
    "datafeed",
    "feed_db",
    "fills",
    "notional",
    "completed_pair_pnl",
    "unpaired_leftover_pnl",
    "paired_locked_edge",
    "max_abs_raw_net_contracts",
    "profit_factor",
    "sharpe",
    "max_drawdown",
    "settlement_source",
    "fill_timing",
    "settlements_from_feed_db",
    "strategy_params",
    "candidate",
    "hypothesis",
    "mechanism",
    "falsification",
    "gate_passed",
    "gate_reasons",
    "report_path",
    "run_dir",
]


@dataclass(frozen=True)
class RunSummary:
    strategy: str
    run_timestamp: str
    run_id: str
    run_dir: str
    feed_db: str
    exchange: str
    datafeed: str
    fills: int
    notional: float
    realized_pnl: float
    completed_pair_pnl: float
    unpaired_leftover_pnl: float
    paired_locked_edge: float
    max_abs_raw_net_contracts: float
    profit_factor: float
    sharpe: float
    max_drawdown: float
    settlement_source: str
    fill_timing: str
    settlements_from_feed_db: bool
    strategy_params: dict[str, Any]
    candidate: str = ""
    hypothesis: str = ""
    mechanism: str = ""
    falsification: str = ""
    gate_passed: str = ""
    gate_reasons: list[str] | None = None
    report_path: str = ""

    def csv_row(self) -> dict[str, str]:
        return {
            "run_date": _run_date(self.run_timestamp),
            "strategy": self.strategy,
            "realized_pnl": _number_text(self.realized_pnl),
            "run_timestamp": self.run_timestamp,
            "run_id": self.run_id,
            "exchange": self.exchange,
            "datafeed": self.datafeed,
            "feed_db": self.feed_db,
            "fills": str(self.fills),
            "notional": _number_text(self.notional),
            "completed_pair_pnl": _number_text(self.completed_pair_pnl),
            "unpaired_leftover_pnl": _number_text(self.unpaired_leftover_pnl),
            "paired_locked_edge": _number_text(self.paired_locked_edge),
            "max_abs_raw_net_contracts": _number_text(self.max_abs_raw_net_contracts),
            "profit_factor": _number_text(self.profit_factor),
            "sharpe": _number_text(self.sharpe),
            "max_drawdown": _number_text(self.max_drawdown),
            "settlement_source": self.settlement_source,
            "fill_timing": self.fill_timing,
            "settlements_from_feed_db": str(self.settlements_from_feed_db).lower(),
            "strategy_params": json.dumps(self.strategy_params, sort_keys=True),
            "candidate": self.candidate,
            "hypothesis": self.hypothesis,
            "mechanism": self.mechanism,
            "falsification": self.falsification,
            "gate_passed": self.gate_passed,
            "gate_reasons": "; ".join(self.gate_reasons or []),
            "report_path": self.report_path,
            "run_dir": self.run_dir,
        }


@dataclass(frozen=True)
class JournalResult:
    run_count: int
    index_path: Path
    history_path: Path
    latest_report_path: Path


def collect_run_summaries(runs_dir: Path | str) -> list[RunSummary]:
    root = Path(runs_dir)
    summaries: list[RunSummary] = []
    for metrics_path in sorted(root.glob("*/*/metrics.json")):
        try:
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(metrics, dict):
            continue
        run_dir = metrics_path.parent
        config = _read_config(run_dir / "config.toml")
        research_summary = _read_research_summary(run_dir / "research_summary.json")
        summaries.append(_summary_from_metrics(metrics, config=config, research_summary=research_summary, run_dir=run_dir))
    return _sort_summaries(summaries)


def build_research_journal(*, runs_dir: Path | str, out_dir: Path | str, top: int = 25) -> JournalResult:
    out = Path(out_dir)
    reports_dir = out / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    summaries = collect_run_summaries(runs_dir)

    index_path = out / "run_index.csv"
    _write_index(index_path, summaries)

    history_path = out / "strategy_history.md"
    history_text = _render_strategy_history(summaries, top=top)
    history_path.write_text(history_text, encoding="utf-8")

    latest_path = reports_dir / "latest.md"
    latest_path.write_text(_render_latest_report(summaries, top=top), encoding="utf-8")

    return JournalResult(
        run_count=len(summaries),
        index_path=index_path,
        history_path=history_path,
        latest_report_path=latest_path,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kbtc-research-journal",
        description="Generate Git-friendly research summaries from local replay run metrics.",
    )
    parser.add_argument("--runs-dir", default="runs", help="Replay runs root. Default: runs")
    parser.add_argument("--out-dir", default="research", help="Research output directory. Default: research")
    parser.add_argument(
        "--top",
        type=int,
        default=25,
        help="Runs per strategy/report. Use 0 to include all runs. Default: 25",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable result summary")
    args = parser.parse_args(argv)

    result = build_research_journal(runs_dir=args.runs_dir, out_dir=args.out_dir, top=args.top)
    if args.json:
        print(
            json.dumps(
                {
                    "run_count": result.run_count,
                    "index_path": str(result.index_path),
                    "history_path": str(result.history_path),
                    "latest_report_path": str(result.latest_report_path),
                },
                sort_keys=True,
            )
        )
    else:
        print(
            "kbtc_research_journal "
            f"runs={result.run_count} index={result.index_path} history={result.history_path} "
            f"latest={result.latest_report_path}"
        )
    return 0


def _summary_from_metrics(
    metrics: dict[str, Any], *, config: dict[str, Any], research_summary: dict[str, Any], run_dir: Path
) -> RunSummary:
    portfolio = _mapping(metrics.get("portfolio_settlement"))
    pnl_split = _mapping(portfolio.get("pnl_split"))
    institutional = _mapping(metrics.get("institutional_metrics"))
    candidate = _mapping(research_summary.get("candidate"))
    gates = _mapping(research_summary.get("gates"))
    feed_db = str(metrics.get("feed_db") or config.get("feed_db") or "")
    exchange, datafeed = _classify_datafeed(feed_db)
    return RunSummary(
        strategy=str(metrics.get("strategy") or config.get("strategy") or run_dir.parent.name),
        run_id=str(metrics.get("run_id") or config.get("run_id") or run_dir.name),
        run_timestamp=_extract_run_timestamp(str(metrics.get("run_id") or config.get("run_id") or run_dir.name)),
        run_dir=str(metrics.get("run_dir") or run_dir),
        feed_db=feed_db,
        exchange=exchange,
        datafeed=datafeed,
        fills=int(float(metrics.get("fills") or 0)),
        notional=float(metrics.get("notional") or 0.0),
        realized_pnl=float(portfolio.get("realized_pnl") or institutional.get("total_pnl") or 0.0),
        completed_pair_pnl=float(pnl_split.get("completed_pair_pnl") or 0.0),
        unpaired_leftover_pnl=float(pnl_split.get("unpaired_leftover_pnl") or 0.0),
        paired_locked_edge=float(portfolio.get("paired_locked_edge") or 0.0),
        max_abs_raw_net_contracts=float(portfolio.get("max_abs_raw_net_contracts") or 0.0),
        profit_factor=float(institutional.get("profit_factor") or 0.0),
        sharpe=float(institutional.get("sharpe") or 0.0),
        max_drawdown=float(institutional.get("max_drawdown") or 0.0),
        settlement_source=str(institutional.get("settlement_source") or ""),
        fill_timing=str(config.get("fill_timing") or ""),
        settlements_from_feed_db=bool(config.get("settlements_from_feed_db") or False),
        strategy_params=_mapping(config.get("strategy_params")),
        candidate=str(candidate.get("name") or config.get("candidate_name") or ""),
        hypothesis=str(candidate.get("economic_story") or ""),
        mechanism=str(candidate.get("mechanism") or ""),
        falsification=str(candidate.get("falsification") or ""),
        gate_passed=_gate_passed_text(gates.get("passed")),
        gate_reasons=_string_list(gates.get("reasons")),
        report_path=str(research_summary.get("report_path") or ""),
    )


def _write_index(path: Path, summaries: list[RunSummary]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        for summary in summaries:
            writer.writerow(summary.csv_row())


def _render_strategy_history(summaries: list[RunSummary], *, top: int) -> str:
    generated_at = datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    lines = [
        "# Strategy Research History",
        "",
        f"Generated: {generated_at}",
        "",
        "Safety boundary: replay/research summaries only; no live orders.",
        "",
    ]
    if not summaries:
        lines += ["No replay runs found.", ""]
        return "\n".join(lines)

    for strategy in sorted({summary.strategy for summary in summaries}):
        strategy_rows = [summary for summary in summaries if summary.strategy == strategy]
        rendered_rows = _limit_rows(strategy_rows, top)
        lines += [
            f"## {strategy}",
            "",
            f"Runs scanned: {len(strategy_rows)}",
            "",
            "### Runs by date, strategy, then realized PnL",
            "",
        ]
        for idx, summary in enumerate(rendered_rows, start=1):
            lines += _render_run_bullets(summary, prefix=f"{idx}. ")
        lines.append("")
    return "\n".join(lines)


def _render_latest_report(summaries: list[RunSummary], *, top: int) -> str:
    generated_at = datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    lines = [
        "# Latest Strategy Replay Report",
        "",
        f"Generated: {generated_at}",
        "",
        "Safety boundary: replay/research summaries only; no live orders.",
        "",
        f"Runs scanned: {len(summaries)}",
        "",
    ]
    if not summaries:
        lines += ["No replay runs found.", ""]
        return "\n".join(lines)
    lines += ["## Runs by date, strategy, then realized PnL", ""]
    for idx, summary in enumerate(_limit_rows(summaries, top), start=1):
        lines += _render_run_bullets(summary, prefix=f"{idx}. ")
    return "\n".join(lines)


def _render_run_bullets(summary: RunSummary, *, prefix: str = "- ") -> list[str]:
    lines = [
        f"{prefix}`{summary.run_id}`",
        f"   - Run timestamp: {summary.run_timestamp or 'unknown'}",
        f"   - Strategy: `{summary.strategy}`",
        f"   - Exchange: {summary.exchange}",
        f"   - Datafeed: {summary.datafeed}",
        f"   - Feed DB: `{summary.feed_db}`",
        f"   - Realized PnL: {_money(summary.realized_pnl)}",
        f"   - Fills: {summary.fills:,}",
        f"   - Notional: {_money(summary.notional)}",
        f"   - Completed-pair PnL: {_money(summary.completed_pair_pnl)}",
        f"   - Unpaired-leftover PnL: {_money(summary.unpaired_leftover_pnl)}",
        f"   - Profit factor: {_number_text(summary.profit_factor)}",
        f"   - Max drawdown: {_money(summary.max_drawdown)}",
    ]
    if summary.strategy_params:
        lines.append("   - Params:")
        for key, value in sorted(summary.strategy_params.items()):
            lines.append(f"     - {key} = {value}")
    if summary.candidate:
        lines.append(f"   - Candidate: `{summary.candidate}`")
    if summary.hypothesis:
        lines.append(f"   - Hypothesis: {summary.hypothesis}")
    if summary.mechanism:
        lines.append(f"   - Mechanism: {summary.mechanism}")
    if summary.falsification:
        lines.append(f"   - Falsification: {summary.falsification}")
    if summary.gate_passed:
        lines.append(f"   - Gate passed: {summary.gate_passed}")
    if summary.gate_reasons:
        lines.append("   - Gate reasons:")
        for reason in summary.gate_reasons:
            lines.append(f"     - {reason}")
    if summary.report_path:
        lines.append(f"   - Research report: `{summary.report_path}`")
    lines.append(f"   - Raw run: `{summary.run_dir}`")
    return lines


_RUN_TIMESTAMP_PATTERN = re.compile(r"(20\d{6}T\d{6}Z|20\d{6}T\d{4}Z|20\d{6}|(?<!\d)1[67]\d{8}(?!\d))")


def _extract_run_timestamp(run_id: str) -> str:
    match = _RUN_TIMESTAMP_PATTERN.search(run_id)
    if not match:
        return ""
    raw = match.group(1)
    if raw.isdigit() and len(raw) == 10:
        return datetime.fromtimestamp(int(raw), tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if len(raw) == 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    date = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    time = raw[9:-1]
    if len(time) == 4:
        time = f"{time[:2]}:{time[2:4]}:00"
    else:
        time = f"{time[:2]}:{time[2:4]}:{time[4:6]}"
    return f"{date}T{time}Z"


def _sort_summaries(summaries: list[RunSummary]) -> list[RunSummary]:
    # Sort by research date first so old pre-settlement/proxy PnL does not
    # dominate the journal. Within each day, group comparable strategy families
    # together and keep a PnL leaderboard inside each strategy.
    rows = list(summaries)
    rows.sort(key=lambda summary: summary.run_id)
    rows.sort(key=lambda summary: summary.run_timestamp, reverse=True)
    rows.sort(key=lambda summary: summary.realized_pnl, reverse=True)
    rows.sort(key=lambda summary: summary.strategy)
    rows.sort(key=lambda summary: _run_date(summary.run_timestamp), reverse=True)
    return rows


def _run_date(run_timestamp: str) -> str:
    return run_timestamp[:10] if run_timestamp else ""


def _limit_rows(rows: list[RunSummary], top: int) -> list[RunSummary]:
    if top <= 0:
        return rows
    return rows[:top]


def _read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    config: dict[str, Any] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        config[key.strip()] = _parse_config_value(raw_value.strip())
    return config


def _parse_config_value(raw_value: str) -> Any:
    if raw_value.lower() == "true":
        return True
    if raw_value.lower() == "false":
        return False
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        pass
    try:
        return float(raw_value)
    except ValueError:
        return raw_value.strip('"')


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _classify_datafeed(feed_db: str) -> tuple[str, str]:
    name = Path(feed_db).name.lower()
    text = feed_db.lower()
    if "polymarket" in text or name.startswith("poly"):
        exchange = "polymarket"
    elif "kalshi" in text or "kbtc" in text:
        exchange = "kalshi"
    else:
        exchange = "unknown"

    if name.endswith(".sqlite3"):
        datafeed = name.removesuffix(".sqlite3")
    elif name:
        datafeed = name
    else:
        datafeed = "unknown"
    return exchange, datafeed


def _money(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.2f}"


def _number_text(value: float) -> str:
    if float(value).is_integer():
        return f"{value:.1f}" if value else "0"
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _read_research_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _gate_passed_text(value: Any) -> str:
    if value is None:
        return ""
    return str(bool(value)).lower()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


if __name__ == "__main__":
    raise SystemExit(main())
