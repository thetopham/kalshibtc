from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from test_feed_replay_architecture import _write_feed_db

from kalshibtc import dashboard
from kalshibtc.replay.cli import main as replay_main
from kalshibtc.strategy.registry import create_strategy, strategy_names

EXPECTED_STRATEGIES = {
    "simple_directional",
    "mean_reversion_to_strike",
    "breakout_momentum",
    "late_window_only",
    "spread_aware_momentum",
    "contrarian_spread_reversion",
    "pair_arb",
    "pair_arb_grid",
    "pair_arb_passive",
    "inventory_vol_rebalance",
    "inventory_vol_regime",
    "volatility_hedge",
    "no_trade_baseline",
}


def test_strategy_registry_lists_initial_ideation_strategies_and_rejects_unknown() -> None:
    assert set(strategy_names()) == EXPECTED_STRATEGIES

    for name in EXPECTED_STRATEGIES:
        strategy = create_strategy(name)
        assert strategy.name == name

    with pytest.raises(ValueError, match="unknown strategy"):
        create_strategy("paper_ledger_legacy")


def test_replay_cli_writes_each_strategy_under_own_run_directory(tmp_path: Path) -> None:
    feed_db = tmp_path / "feed" / "kalshi-btc-1s.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_feed_db(feed_db)

    assert replay_main([
        "--feed-db", str(feed_db),
        "--runs-dir", str(runs_dir),
        "--strategy", "no_trade_baseline",
        "--run-id", "control-run",
        "--json",
    ]) == 0

    run_dir = runs_dir / "no_trade_baseline" / "control-run"
    assert (run_dir / "config.toml").is_file()
    assert (run_dir / "metrics.json").is_file()
    assert (run_dir / "results.sqlite3").is_file()

    metrics = json.loads((run_dir / "metrics.json").read_text())
    assert metrics["strategy"] == "no_trade_baseline"
    assert metrics["run_id"] == "control-run"
    assert metrics["signals"] == 0
    assert metrics["fills"] == 0

    with sqlite3.connect(run_dir / "results.sqlite3") as conn:
        signal_rows = conn.execute("SELECT COUNT(*) FROM replay_signals").fetchone()[0]
        fill_rows = conn.execute("SELECT COUNT(*) FROM replay_fills").fetchone()[0]
    assert signal_rows >= 1
    assert fill_rows == 0


def test_strategy_runs_dashboard_scans_comparison_payload_and_ignores_incomplete_dirs(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    good = runs_dir / "simple_directional" / "run-a"
    bad = runs_dir / "simple_directional" / "broken"
    good.mkdir(parents=True)
    bad.mkdir(parents=True)
    (good / "config.toml").write_text('strategy = "simple_directional"\nrun_id = "run-a"\n')
    (good / "metrics.json").write_text(json.dumps({
        "strategy": "simple_directional",
        "run_id": "run-a",
        "snapshots": 3,
        "signals": 2,
        "fills": 1,
        "notional": 25.0,
        "institutional_metrics": {
            "win_rate": 0.5,
            "ev_per_trade": 1.25,
            "max_drawdown": 2.0,
            "sharpe": 1.1,
            "profit_factor": 2.5,
            "settlement_source": "replay_final_snapshot",
        },
    }))
    with sqlite3.connect(good / "results.sqlite3") as conn:
        conn.execute("CREATE TABLE replay_signals (id INTEGER PRIMARY KEY, ts TEXT, strategy TEXT, side TEXT, confidence REAL, reason TEXT, allowed INTEGER, blocked_by_json TEXT, raw_json TEXT)")
        conn.execute("CREATE TABLE replay_fills (id INTEGER PRIMARY KEY, ts TEXT, strategy TEXT, side TEXT, entry_price REAL, contracts REAL, notional REAL, raw_json TEXT)")
        conn.execute("INSERT INTO replay_signals (ts, strategy, side, confidence, reason, allowed, blocked_by_json, raw_json) VALUES ('2026-05-15T12:00:00+00:00', 'simple_directional', 'long_above', 0.6, 'above strike + trend up', 1, '[]', '{}')")
        conn.execute("INSERT INTO replay_signals (ts, strategy, side, confidence, reason, allowed, blocked_by_json, raw_json) VALUES ('2026-05-15T12:00:01+00:00', 'simple_directional', 'long_above', 0.6, 'blocked', 0, '[\"max_open_positions\"]', '{}')")
        conn.execute("INSERT INTO replay_signals (ts, strategy, side, confidence, reason, allowed, blocked_by_json, raw_json) VALUES ('2026-05-15T12:00:02+00:00', 'simple_directional', 'long_above', 0.6, 'blocked', 0, '[\"max_open_positions\", \"spread_too_wide\"]', '{}')")
        conn.execute("INSERT INTO replay_signals (ts, strategy, side, confidence, reason, allowed, blocked_by_json, raw_json) VALUES ('2026-05-15T12:00:03+00:00', 'simple_directional', 'long_above', 0.6, 'blocked', 0, '[\"max_open_positions\"]', '{}')")
        conn.execute("INSERT INTO replay_fills (ts, strategy, side, entry_price, contracts, notional, raw_json) VALUES ('2026-05-15T12:00:00+00:00', 'simple_directional', 'long_above', 0.55, 45.45, 25.0, '{}')")
    (bad / "metrics.json").write_text("not-json")

    data = dashboard.collect_strategy_runs_dashboard_data(runs_dir=runs_dir)

    assert data["title"] == "Kalshi BTC Strategy Runs"
    assert data["api_path"] == "/api/strategies"
    assert data["runs_dir_path"] == str(runs_dir)
    assert len(data["runs"]) == 1
    assert data["runs"][0]["strategy"] == "simple_directional"
    assert data["runs"][0]["run_id"] == "run-a"
    assert data["runs"][0]["href"] == "/strategy/simple_directional/run-a"
    assert data["runs"][0]["institutional_metrics"]["win_rate"] == 0.5
    assert data["runs"][0]["win_rate"] == 0.5
    assert data["runs"][0]["ev_per_trade"] == 1.25
    assert data["runs"][0]["max_drawdown"] == 2.0
    assert data["ignored_runs"][0]["run_id"] == "broken"

    detail = dashboard.collect_strategy_run_detail_data(
        runs_dir=runs_dir,
        strategy="simple_directional",
        run_id="run-a",
    )
    assert detail["run"]["strategy"] == "simple_directional"
    assert detail["run"]["blockers"]["max_open_positions"] == 3
    assert detail["signals"][0]["side"] == "long_above"
    assert detail["fills"][0]["notional"] == 25.0


def test_strategy_runs_overview_does_not_open_each_results_db_for_blockers(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "simple_directional" / "run-a"
    run_dir.mkdir(parents=True)
    (run_dir / "config.toml").write_text('strategy = "simple_directional"\nrun_id = "run-a"\n')
    (run_dir / "metrics.json").write_text(json.dumps({
        "strategy": "simple_directional",
        "run_id": "run-a",
        "snapshots": 3,
        "signals": 2,
        "fills": 1,
    }))
    # The overview page should be metadata-only. It must not open every
    # results.sqlite3 just to compute blocker counts, because that makes the
    # /strategies dashboard path scale with the number and size of old runs.
    (run_dir / "results.sqlite3").write_text("not a sqlite database")

    data = dashboard.collect_strategy_runs_dashboard_data(runs_dir=runs_dir)

    assert len(data["runs"]) == 1
    assert data["runs"][0]["run_id"] == "run-a"
    assert data["runs"][0]["blockers"] == {}
    assert data["ignored_runs"] == []



def test_strategy_runs_dashboard_html_has_comparison_and_drilldown_landmarks(tmp_path: Path) -> None:
    data = {
        "title": "Kalshi BTC Strategy Runs",
        "api_path": "/api/strategies",
        "boundary": "Read-only replay/backtest dashboard. No order submission.",
        "runs_dir_path": str(tmp_path / "runs"),
        "runs": [
            {
                "strategy": "simple_directional",
                "run_id": "run-a",
                "snapshots": 3,
                "signals": 2,
                "fills": 1,
                "notional": 25.0,
                "win_rate": 0.5,
                "ev_per_trade": 1.25,
                "max_drawdown": 2.0,
                "sharpe": 1.1,
                "profit_factor": 2.5,
                "settlement_source": "replay_final_snapshot",
                "href": "/strategy/simple_directional/run-a",
                "delete_href": "/api/strategies/simple_directional/run-a",
            }
        ],
        "ignored_runs": [],
    }

    html = dashboard.render_strategy_runs_dashboard_html(data)

    assert "Strategy comparison" in html
    assert "id=\"strategy-runs-table\"" in html
    assert "win rate" in html.lower()
    assert "EV/trade" in html
    assert "Sharpe" in html
    assert "Delete" in html
    assert "confirmDeleteRun" in html
    assert "data-method=\"DELETE\"" in html
    assert "/api/strategies" in html
    assert "/strategy/simple_directional/run-a" in html
    assert "paper-ledger.sqlite3" not in html
    assert "live orders" not in html.lower()
    assert "location.reload" not in html

    detail_html = dashboard.render_strategy_run_detail_html({
        "title": "Kalshi BTC Strategy Run",
        "api_path": "/api/strategies/simple_directional/run-a",
        "boundary": "Read-only replay/backtest dashboard. No order submission.",
        "run": data["runs"][0],
        "config_text": 'strategy = "simple_directional"\n',
        "signals": [{"ts": "2026-05-15T12:00:00+00:00", "side": "long_above", "confidence": 0.6, "reason": "above strike + trend up", "allowed": 1}],
        "fills": [{"ts": "2026-05-15T12:00:00+00:00", "side": "long_above", "entry_price": 0.55, "contracts": 45.45, "notional": 25.0}],
    })
    assert "Strategy run drilldown" in detail_html
    assert "win rate" in detail_html.lower()
    assert "EV/trade" in detail_html
    assert "id=\"strategy-signals-table\"" in detail_html
    assert "id=\"strategy-fills-table\"" in detail_html
    assert "above strike + trend up" in detail_html


def test_strategy_run_delete_removes_only_requested_run_directory(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    target = runs_dir / "simple_directional" / "delete-me"
    sibling = runs_dir / "simple_directional" / "keep-me"
    for run_dir in (target, sibling):
        run_dir.mkdir(parents=True)
        (run_dir / "config.toml").write_text('strategy = "simple_directional"\n')
        (run_dir / "metrics.json").write_text(json.dumps({"strategy": "simple_directional", "run_id": run_dir.name}))
        with sqlite3.connect(run_dir / "results.sqlite3") as conn:
            conn.execute("CREATE TABLE replay_signals (id INTEGER PRIMARY KEY, ts TEXT, strategy TEXT, side TEXT, confidence REAL, reason TEXT, allowed INTEGER, blocked_by_json TEXT, raw_json TEXT)")
            conn.execute("CREATE TABLE replay_fills (id INTEGER PRIMARY KEY, ts TEXT, strategy TEXT, side TEXT, entry_price REAL, contracts REAL, notional REAL, raw_json TEXT)")

    result = dashboard.delete_strategy_run(runs_dir=runs_dir, strategy="simple_directional", run_id="delete-me")

    assert result["deleted"] is True
    assert result["strategy"] == "simple_directional"
    assert result["run_id"] == "delete-me"
    assert not target.exists()
    assert sibling.exists()


def test_strategy_run_delete_rejects_live_slot_and_path_traversal(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    live = runs_dir / "live" / "simple_directional"
    live.mkdir(parents=True)

    with pytest.raises(ValueError, match="live strategy slots"):
        dashboard.delete_strategy_run(runs_dir=runs_dir, strategy="simple_directional", run_id="live")
    with pytest.raises(ValueError, match="unsafe"):
        dashboard.delete_strategy_run(runs_dir=runs_dir, strategy="..", run_id="escape")

    assert live.exists()
