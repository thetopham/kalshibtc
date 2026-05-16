from __future__ import annotations

import json
import sqlite3
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from test_feed_replay_architecture import _write_feed_db

from kalshibtc.backtest.metrics import compute_metrics
from kalshibtc.replay.cli import backfill_market_settlements, main as replay_main
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
class _MetricFill:
    pnl: float | None = None
    notional: float | None = None


def test_compute_metrics_matches_institutional_semantics_for_dict_fills() -> None:
    fills = [
        {"pnl": 12.0, "notional": 100.0},
        {"pnl": -4.0, "notional": 100.0},
        {"pnl": 8.0, "notional": 100.0},
        {"pnl": -2.0, "notional": 100.0},
        {"pnl": 6.0, "notional": 100.0},
    ]

    metrics = compute_metrics(fills, annualization=252, initial_capital=100.0)

    assert metrics == {
        "trades": 5,
        "wins": 3,
        "losses": 2,
        "win_rate": 0.6,
        "total_pnl": 20.0,
        "ev_per_trade": 4.0,
        "max_drawdown": 4.0,
        "max_drawdown_pct": 0.04,
        "sharpe": 9.362273971265926,
        "sortino": 44.8998886412873,
        "calmar": 251.99999999999994,
        "volatility": 1.0766615066955818,
        "mean_return": 0.039999999999999994,
        "skewness": -0.12900922305569712,
        "kurtosis": 1.4413988657844985,
        "excess_kurtosis": -1.5586011342155015,
        "profit_factor": 26.0 / 6.0,
        "avg_win": 26.0 / 3.0,
        "avg_loss": -3.0,
        "largest_win": 12.0,
        "largest_loss": -4.0,
        "gross_profit": 26.0,
        "gross_loss": -6.0,
        "notional": 500.0,
    }


def test_compute_metrics_supports_dataclass_like_fills() -> None:
    fills = [
        _MetricFill(pnl=10.0, notional=25.0),
        _MetricFill(pnl=-5.0, notional=75.0),
        _MetricFill(pnl=0.0, notional=50.0),
    ]

    metrics = compute_metrics(fills, annualization=252, initial_capital=100.0)

    assert metrics["trades"] == 3
    assert metrics["wins"] == 1
    assert metrics["losses"] == 1
    assert metrics["total_pnl"] == 5.0
    assert metrics["gross_profit"] == 10.0
    assert metrics["gross_loss"] == -5.0
    assert metrics["notional"] == 150.0
    assert metrics["profit_factor"] == 2.0


def test_compute_metrics_empty_and_low_sample_behavior_is_safe() -> None:
    empty = compute_metrics([])
    one_winner = compute_metrics([{"pnl": 7.0, "notional": 50.0}])
    one_loser = compute_metrics([{"pnl": -3.0, "notional": 25.0}])
    zero_capital = compute_metrics([{"pnl": 7.0, "notional": 50.0}], initial_capital=0.0)

    assert empty["trades"] == 0
    assert empty["notional"] == 0
    assert empty["profit_factor"] == 0.0
    assert empty["sharpe"] == 0.0
    assert empty["sortino"] == 0.0
    assert empty["calmar"] == 0.0
    assert empty["volatility"] == 0.0
    assert empty["skewness"] == 0.0
    assert empty["kurtosis"] == 0.0
    assert empty["excess_kurtosis"] == 0.0

    assert one_winner["profit_factor"] == math.inf
    assert one_winner["avg_loss"] == 0.0
    assert one_winner["largest_loss"] == 0.0
    assert one_winner["sharpe"] == 0.0
    assert one_winner["sortino"] == 0.0
    assert one_winner["calmar"] == 0.0

    assert one_loser["profit_factor"] == 0.0
    assert one_loser["avg_win"] == 0.0
    assert one_loser["largest_win"] == 0.0
    assert one_loser["max_drawdown"] == 3.0
    assert one_loser["max_drawdown_pct"] == 0.03

    assert zero_capital["mean_return"] == 0.0
    assert zero_capital["max_drawdown_pct"] == 0.0
    assert zero_capital["volatility"] == 0.0


def test_replay_cli_prefers_official_market_settlement_table_over_final_snapshot(tmp_path: Path) -> None:
    feed_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_feed_db(feed_db)
    with sqlite3.connect(feed_db) as conn:
        conn.execute(
            """
            CREATE TABLE market_settlements (
                market_ticker TEXT PRIMARY KEY,
                settlement_source TEXT NOT NULL,
                settlement_result TEXT NOT NULL,
                official_result TEXT,
                official_expiration_value REAL,
                settlement_value_dollars REAL,
                settlement_raw_json TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO market_settlements (
                market_ticker, settlement_source, settlement_result, official_result,
                official_expiration_value, settlement_value_dollars, settlement_raw_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "KXBTC15M-TEST",
                "kalshi_official",
                "below",
                "no",
                99_900.0,
                1.0,
                '{"result":"no"}',
                datetime(2026, 5, 15, 12, 20, tzinfo=UTC).isoformat(),
            ),
        )

    assert replay_main(
        [
            "--feed-db",
            str(feed_db),
            "--runs-dir",
            str(runs_dir),
            "--strategy",
            "simple_directional",
            "--run-id",
            "official-override",
            "--max-open-positions",
            "10",
            "--json",
        ]
    ) == 0

    metrics = json.loads(
        (runs_dir / "simple_directional" / "official-override" / "metrics.json").read_text()
    )

    assert metrics["institutional_metrics"]["settlement_source"] == "kalshi_official"
    assert metrics["institutional_metrics"]["settled_trades"] == metrics["fills"]
    assert "kalshi_official" in metrics["institutional_metrics"]["settlement_sources"]
    assert "replay_final_snapshot" not in metrics["institutional_metrics"]["settlement_sources"]


def test_backfill_market_settlements_writes_official_results_without_snapshot_duplication(
    tmp_path: Path,
) -> None:
    feed_db = tmp_path / "feed.sqlite3"
    _write_feed_db(feed_db)

    class FakeOfficialClient:
        def get_market(self, market_ticker: str) -> dict[str, object]:
            return {
                "ticker": market_ticker,
                "result": "yes",
                "expiration_value": 100_123.45,
                "settlement_value_dollars": 1.0,
            }

    count = backfill_market_settlements(feed_db, official_client=FakeOfficialClient())

    assert count == 1
    with sqlite3.connect(feed_db) as conn:
        row = conn.execute("SELECT * FROM market_settlements WHERE market_ticker = 'KXBTC15M-TEST'").fetchone()
        snapshot_columns = {item[1] for item in conn.execute("PRAGMA table_info(realtime_snapshots_1s)")}

    assert row is not None
    assert "settlement_source" not in snapshot_columns
    assert "official_result" not in snapshot_columns


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
