from __future__ import annotations

import csv
import json
from pathlib import Path

from kalshibtc.research.journal import build_research_journal, collect_run_summaries


def _write_run(
    runs_dir: Path,
    *,
    strategy: str,
    run_id: str,
    realized_pnl: float,
    completed_pair_pnl: float = 0.0,
    unpaired_leftover_pnl: float = 0.0,
    profit_factor: float = 1.0,
    max_drawdown: float = 0.0,
    strategy_params: dict[str, object] | None = None,
    candidate_summary: dict[str, object] | None = None,
) -> Path:
    run_dir = runs_dir / strategy / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "config.toml").write_text(
        "\n".join(
            [
                'feed_db = "feed/kalshi-btc-1s.sqlite3"',
                f'strategy = "{strategy}"',
                f'run_id = "{run_id}"',
                "base_size_dollars = 1000.0",
                "max_position_dollars = 1000.0",
                "max_open_positions = 999",
                "max_spread = 1.0",
                f"strategy_params = {json.dumps(strategy_params or {}, sort_keys=True)}",
                "settlements_from_feed_db = true",
                'fill_timing = "next-tick"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    metrics = {
        "feed_db": "feed/kalshi-btc-1s.sqlite3",
        "run_dir": str(run_dir),
        "run_id": run_id,
        "strategy": strategy,
        "snapshots": 123,
        "signals": 45,
        "fills": 10,
        "notional": 250.5,
        "portfolio_settlement": {
            "realized_pnl": realized_pnl,
            "pnl_split": {
                "completed_pair_pnl": completed_pair_pnl,
                "unpaired_leftover_pnl": unpaired_leftover_pnl,
            },
            "paired_locked_edge": 12.34,
            "max_abs_raw_net_contracts": 5.0,
        },
        "institutional_metrics": {
            "profit_factor": profit_factor,
            "sharpe": 0.42,
            "max_drawdown": max_drawdown,
            "settlement_source": "kalshi_api",
        },
    }
    (run_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    if candidate_summary is not None:
        (run_dir / "research_summary.json").write_text(json.dumps(candidate_summary), encoding="utf-8")
    return run_dir


def test_collect_run_summaries_flattens_metrics_and_config_params(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    _write_run(
        runs_dir,
        strategy="seed_cheap_accumulate_repair_v1",
        run_id="tune_90_30_r360_net0.45-20260518T065339Z",
        realized_pnl=595.72,
        completed_pair_pnl=2127.09,
        unpaired_leftover_pnl=-1531.37,
        profit_factor=1.041,
        max_drawdown=88.12,
        strategy_params={"repair_start_seconds": 360, "max_net_ratio": 0.45},
    )

    summaries = collect_run_summaries(runs_dir)

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.strategy == "seed_cheap_accumulate_repair_v1"
    assert summary.run_id == "tune_90_30_r360_net0.45-20260518T065339Z"
    assert summary.realized_pnl == 595.72
    assert summary.completed_pair_pnl == 2127.09
    assert summary.unpaired_leftover_pnl == -1531.37
    assert summary.profit_factor == 1.041
    assert summary.max_drawdown == 88.12
    assert summary.feed_db == "feed/kalshi-btc-1s.sqlite3"
    assert summary.exchange == "kalshi"
    assert summary.datafeed == "kalshi-btc-1s"
    assert summary.fill_timing == "next-tick"
    assert summary.strategy_params == {"max_net_ratio": 0.45, "repair_start_seconds": 360}


def test_build_research_journal_writes_csv_and_markdown_sorted_by_date(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    out_dir = tmp_path / "research"
    _write_run(
        runs_dir,
        strategy="seed_cheap_accumulate_repair_v1",
        run_id="weak-20260518T060000Z",
        realized_pnl=-10.0,
        profit_factor=0.8,
        strategy_params={"repair_start_seconds": 330},
    )
    _write_run(
        runs_dir,
        strategy="seed_cheap_accumulate_repair_v1",
        run_id="winner-20260517T060000Z",
        realized_pnl=25.0,
        completed_pair_pnl=40.0,
        unpaired_leftover_pnl=-15.0,
        profit_factor=1.2,
        strategy_params={"repair_start_seconds": 360},
    )

    _write_run(
        runs_dir,
        strategy="seed_cheap_accumulate_repair_v1",
        run_id="older-bigger-20260517T070000Z",
        realized_pnl=1000.0,
        profit_factor=2.0,
    )
    _write_run(
        runs_dir,
        strategy="another_strategy",
        run_id="other-later-20260517T080000Z",
        realized_pnl=-1.0,
        profit_factor=0.9,
    )

    result = build_research_journal(runs_dir=runs_dir, out_dir=out_dir, top=10)

    assert result.run_count == 4
    index_path = out_dir / "run_index.csv"
    history_path = out_dir / "strategy_history.md"
    latest_path = out_dir / "reports" / "latest.md"
    assert result.index_path == index_path
    assert result.history_path == history_path
    assert result.latest_report_path == latest_path

    rows = list(csv.DictReader(index_path.read_text(encoding="utf-8").splitlines()))
    assert [row["run_id"] for row in rows] == [
        "weak-20260518T060000Z",
        "other-later-20260517T080000Z",
        "older-bigger-20260517T070000Z",
        "winner-20260517T060000Z",
    ]
    assert list(rows[0].keys())[:3] == ["run_date", "strategy", "realized_pnl"]
    assert rows[0]["run_date"] == "2026-05-18"
    assert rows[0]["run_timestamp"] == "2026-05-18T06:00:00Z"
    assert rows[0]["exchange"] == "kalshi"
    assert rows[0]["datafeed"] == "kalshi-btc-1s"
    assert rows[0]["feed_db"] == "feed/kalshi-btc-1s.sqlite3"
    assert rows[3]["strategy_params"] == '{"repair_start_seconds": 360}'
    assert rows[3]["realized_pnl"] == "25.0"

    history = history_path.read_text(encoding="utf-8")
    assert "# Strategy Research History" in history
    assert "## seed_cheap_accumulate_repair_v1" in history
    assert "Runs by date, strategy, then realized PnL" in history
    assert "`winner-20260517T060000Z`" in history
    assert "Run timestamp: 2026-05-17T06:00:00Z" in history
    assert "Exchange: kalshi" in history
    assert "Datafeed: kalshi-btc-1s" in history
    assert "Realized PnL: +$25.00" in history
    assert "repair_start_seconds = 360" in history
    assert "`weak-20260518T060000Z`" in history

    latest = latest_path.read_text(encoding="utf-8")
    assert "# Latest Strategy Replay Report" in latest
    assert "## Runs by date, strategy, then realized PnL" in latest
    assert latest.index("weak-20260518T060000Z") < latest.index("other-later-20260517T080000Z")
    assert latest.index("other-later-20260517T080000Z") < latest.index("older-bigger-20260517T070000Z")
    assert latest.index("older-bigger-20260517T070000Z") < latest.index("winner-20260517T060000Z")
    assert "Safety boundary: replay/research summaries only; no live orders." in latest


def test_research_journal_surfaces_manual_candidate_hypothesis_and_artifacts(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    out_dir = tmp_path / "research"
    run_dir = _write_run(
        runs_dir,
        strategy="candidate_polymarket_late_window_only",
        run_id="manual-poly-late-20260522T120000Z",
        realized_pnl=17.5,
        profit_factor=1.4,
        strategy_params={"max_seconds_to_close": 65, "min_distance": 25},
        candidate_summary={
            "candidate": {
                "name": "polymarket_late_window_only",
                "economic_story": "Polymarket BTC 15m late-window distance from strike persists into resolution.",
                "mechanism": "parameterized_late_window",
                "falsification": "Reject if broad Polymarket replay loses after spread/fees.",
            },
            "gates": {"passed": True, "reasons": ["fills >= 10"]},
            "report_path": "research/manual/poly-late-window.md",
        },
    )

    build_research_journal(runs_dir=runs_dir, out_dir=out_dir, top=10)

    rows = list(csv.DictReader((out_dir / "run_index.csv").read_text(encoding="utf-8").splitlines()))
    assert rows[0]["candidate"] == "polymarket_late_window_only"
    assert rows[0]["hypothesis"] == "Polymarket BTC 15m late-window distance from strike persists into resolution."
    assert rows[0]["falsification"] == "Reject if broad Polymarket replay loses after spread/fees."
    assert rows[0]["gate_passed"] == "true"
    assert rows[0]["report_path"] == "research/manual/poly-late-window.md"

    history = (out_dir / "strategy_history.md").read_text(encoding="utf-8")
    assert "Candidate: `polymarket_late_window_only`" in history
    assert "Hypothesis: Polymarket BTC 15m late-window distance from strike persists into resolution." in history
    assert "Falsification: Reject if broad Polymarket replay loses after spread/fees." in history
    assert "Gate passed: true" in history
    assert "Research report: `research/manual/poly-late-window.md`" in history
    assert f"Raw run: `{run_dir}`" in history
