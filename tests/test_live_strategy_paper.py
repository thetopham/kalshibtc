from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from test_kalshibtc_1s_paper_executor import _snapshot_db

from kalshibtc.live_strategy_paper import (
    MultiStrategyPaperRunSummary,
    run_live_strategy_paper_once,
)
from kalshibtc.paper_signal_executor import main as paper_main


def _iso(seconds: int) -> str:
    return datetime(2026, 5, 15, 12, 0, seconds, tzinfo=UTC).isoformat()


def _write_live_snapshot_db(path: Path) -> None:
    close_time = datetime(2026, 5, 15, 12, 10, tzinfo=UTC)
    _snapshot_db(
        path,
        rows=[
            {
                "ts": _iso(10),
                "market_ticker": "KXBTC15M-LIVE-A",
                "market_close_time": close_time.isoformat(),
                "btc_price": 100_020.0,
                "strike": 100_000.0,
                "btc_velocity_30s": 2.0,
                "yes_bid": 0.52,
                "yes_ask": 0.54,
                "no_bid": 0.44,
                "no_ask": 0.46,
            },
            {
                "ts": _iso(20),
                "market_ticker": "KXBTC15M-LIVE-B",
                "market_close_time": close_time.isoformat(),
                "btc_price": 99_940.0,
                "strike": 100_000.0,
                "btc_velocity_30s": 0.2,
                "yes_bid": 0.42,
                "yes_ask": 0.44,
                "no_bid": 0.54,
                "no_ask": 0.56,
            },
        ],
    )


def test_live_multi_strategy_paper_writes_one_results_db_per_strategy(tmp_path: Path) -> None:
    snapshot_db = tmp_path / "feed" / "kalshi-btc-1s.sqlite3"
    snapshot_db.parent.mkdir()
    runs_dir = tmp_path / "runs"
    _write_live_snapshot_db(snapshot_db)

    summary = run_live_strategy_paper_once(
        snapshot_db=snapshot_db,
        runs_dir=runs_dir,
        strategies=["simple_directional", "no_trade_baseline"],
        limit=10,
        no_official_settlement=True,
    )

    assert isinstance(summary, MultiStrategyPaperRunSummary)
    assert summary.mode == "live_paper"
    assert summary.snapshots_db_path == str(snapshot_db)
    assert [item["strategy"] for item in summary.strategies] == [
        "simple_directional",
        "no_trade_baseline",
    ]

    simple_dir = runs_dir / "live" / "simple_directional"
    control_dir = runs_dir / "live" / "no_trade_baseline"
    assert (simple_dir / "config.toml").is_file()
    assert (simple_dir / "metrics.json").is_file()
    assert (simple_dir / "results.sqlite3").is_file()
    assert (control_dir / "config.toml").is_file()
    assert (control_dir / "metrics.json").is_file()
    assert (control_dir / "results.sqlite3").is_file()

    simple_metrics = json.loads((simple_dir / "metrics.json").read_text())
    control_metrics = json.loads((control_dir / "metrics.json").read_text())
    assert simple_metrics["mode"] == "live_paper"
    assert simple_metrics["strategy"] == "simple_directional"
    assert simple_metrics["snapshots_processed"] == 2
    assert simple_metrics["signals_recorded"] == 2
    assert simple_metrics["snapshots"] == 2
    assert simple_metrics["signals"] == 2
    assert simple_metrics["trades_opened"] >= 1
    assert control_metrics["strategy"] == "no_trade_baseline"
    assert control_metrics["trades_opened"] == 0

    with sqlite3.connect(simple_dir / "results.sqlite3") as conn:
        strategies = {row[0] for row in conn.execute("SELECT DISTINCT strategy FROM predictions")}
        simple_trade_count = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    with sqlite3.connect(control_dir / "results.sqlite3") as conn:
        control_trade_count = conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0]
    assert strategies == {"simple_directional"}
    assert simple_trade_count >= 1
    assert control_trade_count == 0


def test_paper_executor_cli_accepts_all_strategies_and_live_multi_strategy_mode(tmp_path: Path) -> None:
    snapshot_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_live_snapshot_db(snapshot_db)

    exit_code = paper_main(
        [
            "--snapshot-db",
            str(snapshot_db),
            "--runs-dir",
            str(runs_dir),
            "--strategy",
            "simple_directional,no_trade_baseline",
            "--limit",
            "10",
            "--json",
            "--no-official-settlement",
        ]
    )

    assert exit_code == 0
    assert (runs_dir / "live" / "simple_directional" / "results.sqlite3").is_file()
    assert (runs_dir / "live" / "no_trade_baseline" / "results.sqlite3").is_file()


def test_volatility_hedge_live_config_writes_balancer_and_lifecycle_knobs(tmp_path: Path) -> None:
    snapshot_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_live_snapshot_db(snapshot_db)

    summary = run_live_strategy_paper_once(
        snapshot_db=snapshot_db,
        runs_dir=runs_dir,
        strategies=["volatility_hedge"],
        limit=10,
        no_official_settlement=True,
    )

    assert summary.strategies[0]["strategy"] == "volatility_hedge"
    config_text = (runs_dir / "live" / "volatility_hedge" / "config.toml").read_text()
    for knob in [
        "target_lean_ratio",
        "soft_imbalance_ratio",
        "repair_imbalance_ratio",
        "hard_imbalance_ratio",
        "emergency_imbalance_ratio",
        "require_seed_pair_cost_below",
        "max_initial_ask_sum",
        "allow_seed_loss",
        "base_add_notional",
        "repair_add_notional",
        "max_contracts_per_add",
        "max_notional_per_market",
        "max_notional_per_side",
        "max_worst_case_loss_per_market",
        "max_settlement_ev_worsening",
        "total_contract_seconds",
        "observe_seconds",
        "early_seed_until_seconds",
        "main_harvest_until_seconds",
        "repair_protect_until_seconds",
        "stop_new_seed_seconds_before_expiry",
        "stop_normal_add_seconds_before_expiry",
    ]:
        assert f"{knob} =" in config_text
    assert "require_seed_pair_cost_below = 1.04" in config_text
    assert "max_initial_ask_sum = 1.03" in config_text
    assert "allow_seed_loss = true" in config_text


def test_live_strategy_metrics_are_cumulative_across_loop_passes(tmp_path: Path) -> None:
    snapshot_db = tmp_path / "feed" / "kalshi-btc-1s.sqlite3"
    snapshot_db.parent.mkdir()
    runs_dir = tmp_path / "runs"
    _write_live_snapshot_db(snapshot_db)

    first = run_live_strategy_paper_once(
        snapshot_db=snapshot_db,
        runs_dir=runs_dir,
        strategies=["simple_directional"],
        limit=1,
        no_official_settlement=True,
    )
    second = run_live_strategy_paper_once(
        snapshot_db=snapshot_db,
        runs_dir=runs_dir,
        strategies=["simple_directional"],
        limit=1,
        no_official_settlement=True,
    )

    assert first.strategies[0]["snapshots_processed"] == 1
    assert second.strategies[0]["snapshots_processed"] == 2
    metrics = json.loads((runs_dir / "live" / "simple_directional" / "metrics.json").read_text())
    assert metrics["snapshots_processed"] == 2
    assert metrics["signals_recorded"] == 2
    assert metrics["snapshots"] == 2
    assert metrics["signals"] == 2
