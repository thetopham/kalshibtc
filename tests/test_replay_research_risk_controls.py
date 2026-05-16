from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from test_feed_replay_architecture import _write_feed_db

from kalshibtc.backtest.metrics import compute_metrics
from kalshibtc.replay.cli import main as replay_main
from kalshibtc.replay.settlement import estimate_replay_fill_pnls


def test_replay_cli_accepts_max_open_positions_for_research_runs(tmp_path: Path) -> None:
    feed_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_feed_db(feed_db)

    assert replay_main(
        [
            "--feed-db",
            str(feed_db),
            "--runs-dir",
            str(runs_dir),
            "--strategy",
            "simple_directional",
            "--run-id",
            "wide-open",
            "--max-open-positions",
            "10",
            "--json",
        ]
    ) == 0

    run_dir = runs_dir / "simple_directional" / "wide-open"
    config = (run_dir / "config.toml").read_text()
    metrics = json.loads((run_dir / "metrics.json").read_text())
    with sqlite3.connect(run_dir / "results.sqlite3") as conn:
        fills = conn.execute("SELECT COUNT(*) FROM replay_fills").fetchone()[0]

    assert 'max_open_positions = 10' in config
    assert metrics["max_open_positions"] == 10
    assert "institutional_metrics" in metrics
    assert metrics["institutional_metrics"]["trades"] == fills
    assert "sharpe" in metrics["institutional_metrics"]
    assert "sortino" in metrics["institutional_metrics"]
    assert "calmar" in metrics["institutional_metrics"]
    assert "profit_factor" in metrics["institutional_metrics"]
    assert "max_drawdown_pct" in metrics["institutional_metrics"]
    assert metrics["institutional_metrics"]["settlement_source"] == "replay_final_snapshot"
    assert metrics["institutional_metrics"]["settled_trades"] == fills
    assert "replay_final_snapshot" in metrics["institutional_metrics"]["settlement_sources"]
    assert metrics["institutional_metrics"]["total_pnl"] != 0.0
    assert fills >= 1


def test_compute_metrics_returns_institutional_fields_for_fill_pnls() -> None:
    fills = [
        {"pnl": 12.0, "notional": 100.0},
        {"pnl": -4.0, "notional": 100.0},
        {"pnl": 8.0, "notional": 100.0},
        {"pnl": -2.0, "notional": 100.0},
        {"pnl": 6.0, "notional": 100.0},
    ]

    metrics = compute_metrics(fills, annualization=252)

    assert metrics["trades"] == 5
    assert metrics["wins"] == 3
    assert metrics["losses"] == 2
    assert metrics["notional"] == 500.0
    assert metrics["total_pnl"] == 20.0
    assert metrics["gross_profit"] == 26.0
    assert metrics["gross_loss"] == -6.0
    assert metrics["profit_factor"] == 26.0 / 6.0
    assert metrics["avg_win"] == 26.0 / 3.0
    assert metrics["avg_loss"] == -3.0
    assert metrics["largest_win"] == 12.0
    assert metrics["largest_loss"] == -4.0
    assert metrics["max_drawdown"] == 4.0
    assert metrics["max_drawdown_pct"] == 0.04
    assert metrics["sharpe"] > 0
    assert metrics["sortino"] > 0
    assert metrics["calmar"] > 0
    assert "skewness" in metrics
    assert "excess_kurtosis" in metrics


@dataclass(frozen=True)
class _Fill:
    market_ticker: str
    side: str
    entry_price: float
    notional: float
    contracts: float
    ts: datetime


def test_estimate_replay_fill_pnls_uses_final_snapshot_outcome() -> None:
    fills = [
        _Fill(
            market_ticker="KXBTC15M-TEST",
            side="long_above",
            entry_price=0.40,
            notional=20.0,
            contracts=50.0,
            ts=datetime(2026, 5, 15, 12, 14, 0, tzinfo=UTC),
        ),
        _Fill(
            market_ticker="KXBTC15M-TEST",
            side="long_below",
            entry_price=0.30,
            notional=30.0,
            contracts=100.0,
            ts=datetime(2026, 5, 15, 12, 14, 10, tzinfo=UTC),
        ),
    ]
    settlement_rows = [
        {"market_ticker": "KXBTC15M-TEST", "btc_price": 100_100.0, "strike": 100_000.0},
    ]

    settled = estimate_replay_fill_pnls(fills, settlement_rows)

    assert settled[0]["pnl"] == 30.0
    assert settled[0]["settlement_result"] == "above"
    assert settled[0]["exit_price"] == 1.0
    assert settled[0]["settlement_source"] == "replay_final_snapshot"
    assert settled[1]["pnl"] == -30.0
    assert settled[1]["settlement_result"] == "above"
    assert settled[1]["exit_price"] == 0.0
