from __future__ import annotations

import ast
import sqlite3
import subprocess
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kalshibtc import dashboard
from kalshibtc.record_1s_snapshots import RealtimeSnapshotRecorder
from kalshibtc.storage.paper_signal_store import initialize_results_db

BANNED_DASHBOARD_HTML_TERMS = (
    "WATCH_ONLY_EV_SIGNAL",
    "guarded live IOC",
    "live orders enabled",
    "orders enabled with caps",
    "live-demo",
    "live-prod",
    "kalshi_btc_15m_bot",
    "paper-ledger.sqlite3",
    "old paper scanner",
    "scanner predictions",
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def _make_snapshot_db(path: Path) -> None:
    recorder = RealtimeSnapshotRecorder(path)
    base = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    prices = [100_000.0, 100_020.0, 100_035.0, 100_050.0]
    for seconds_ago, price in zip((60, 30, 10, 0), prices, strict=True):
        recorder.record_snapshot(
            {
                "ts": base - timedelta(seconds=seconds_ago),
                "market_ticker": "KXBTC15M-DASHBOARD",
                "market_open_time": base - timedelta(minutes=10),
                "market_close_time": base + timedelta(seconds=120),
                "btc_price": price,
                "strike": 100_000.0,
                "target_price": 100_000.0,
                "btc_velocity_30s": 1.5,
                "yes_bid": 0.54,
                "yes_ask": 0.56,
                "no_bid": 0.43,
                "no_ask": 0.45,
                "orderbook_sequence": 123,
                "execution_blocked_by": [],
                # These legacy fields are allowed to remain in raw JSON/API payloads,
                # but the main HTML must not present them as current signals.
                "decision": "WATCH_ONLY_EV_SIGNAL",
                "monitor_action": "EV_NO",
                "model_probability_yes": 0.91,
            }
        )


def _make_results_db(path: Path) -> None:
    initialize_results_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO predictions (
                id, created_at, market_ticker, event_ticker, market_close_time,
                strategy, action, side, probability_yes, probability_no, confidence,
                edge, stake_dollars, current_price, target_price, yes_ask, no_ask,
                model_info_json, reasons_json, features_json, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "p-settled",
                "2026-05-15T11:58:00+00:00",
                "KXBTC15M-DASHBOARD",
                "KXBTC15M",
                "2026-05-15T12:00:00+00:00",
                "simple_directional",
                "BUY_YES",
                "YES",
                0.7,
                0.3,
                0.7,
                0.2,
                25.0,
                100_030.0,
                100_000.0,
                0.55,
                0.47,
                '{"name":"1s_simple_directional","trained":false}',
                '["above strike + trend up"]',
                '{"slope_at_entry":2.0,"distance_from_strike":30.0,"seconds_to_expiry":120.0}',
                "{}",
            ),
        )
        conn.execute(
            """
            INSERT INTO predictions (
                id, created_at, market_ticker, event_ticker, market_close_time,
                strategy, action, side, probability_yes, probability_no, confidence,
                edge, stake_dollars, current_price, target_price, yes_ask, no_ask,
                model_info_json, reasons_json, features_json, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "p-open",
                "2026-05-15T11:59:00+00:00",
                "KXBTC15M-DASHBOARD",
                "KXBTC15M",
                "2026-05-15T12:10:00+00:00",
                "simple_directional",
                "BUY_NO",
                "NO",
                0.35,
                0.65,
                0.65,
                0.15,
                25.0,
                99_970.0,
                100_000.0,
                0.48,
                0.52,
                '{"name":"1s_simple_directional","trained":false}',
                '["below strike + trend down", "max_open_positions"]',
                '{"slope_at_entry":-2.0,"distance_from_strike":-30.0,"seconds_to_expiry":660.0}',
                "{}",
            ),
        )
        conn.execute(
            """
            INSERT INTO paper_trades (
                id, prediction_id, created_at, market_ticker, strategy, side,
                entry_price, contracts, notional, status, market_close_time,
                settlement_result, realized_pnl, settled_at, exit_price, exit_reason,
                settlement_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "t-settled",
                "p-settled",
                "2026-05-15T11:58:00+00:00",
                "KXBTC15M-DASHBOARD",
                "simple_directional",
                "YES",
                0.55,
                45.4545,
                25.0,
                "SETTLED",
                "2026-05-15T12:00:00+00:00",
                "above",
                20.4545,
                "2026-05-15T12:00:01+00:00",
                1.0,
                "1s_expiry_above",
                "coinbase_estimate",
            ),
        )
        conn.execute(
            """
            INSERT INTO paper_trades (
                id, prediction_id, created_at, market_ticker, strategy, side,
                entry_price, contracts, notional, status, market_close_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "t-open",
                "p-open",
                "2026-05-15T11:59:00+00:00",
                "KXBTC15M-DASHBOARD",
                "simple_directional",
                "NO",
                0.52,
                48.0769,
                25.0,
                "OPEN",
                "2026-05-15T12:10:00+00:00",
            ),
        )


def test_dashboard_module_imports_without_legacy_package() -> None:
    assert dashboard.__name__ == "kalshibtc.dashboard"


def test_stream_dashboard_uses_clean_1s_state_and_demotes_legacy_fields(tmp_path: Path) -> None:
    snapshot_db = tmp_path / "realtime-snapshots-1s.sqlite3"
    _make_snapshot_db(snapshot_db)

    data = dashboard.collect_stream_dashboard_data(snapshot_db=snapshot_db, history_limit=20)

    assert data["title"] == "Kalshi BTC Stream"
    assert data["api_path"] == "/api/stream"
    assert data["mode"] == "paper/research/read-only"
    latest = data["stream"]["latest"]
    assert latest["market_ticker"] == "KXBTC15M-DASHBOARD"
    assert latest["above_below_strike"] == "above"
    assert latest["slopes"]["slope_10s"] is not None
    assert latest["slopes"]["slope_30s"] == pytest.approx(1.5)
    assert latest["slopes"]["slope_60s"] is not None
    assert latest["execution_decision"]["action"] == "BUY_YES"
    assert latest["execution_decision"]["size_dollars"] > 0
    assert latest["raw_payload"]["decision"] == "WATCH_ONLY_EV_SIGNAL"

    html = dashboard.render_stream_dashboard_html(data)

    assert "Kalshi BTC Stream" in html
    assert "/api/stream" in html
    assert "Execution Decision" in html
    assert "Recent graph points" in html
    assert "YES / NO orderbook" in html
    assert "Raw payload" in html
    for term in BANNED_DASHBOARD_HTML_TERMS:
        assert term not in html


def test_status_dashboard_reads_paper_schema_and_has_required_review_sections(tmp_path: Path) -> None:
    snapshot_db = tmp_path / "realtime-snapshots-1s.sqlite3"
    results_db = tmp_path / "paper-results-1s.sqlite3"
    _make_snapshot_db(snapshot_db)
    _make_results_db(results_db)

    data = dashboard.collect_status_dashboard_data(
        snapshot_db=snapshot_db,
        results_db=results_db,
        service_status_fn=lambda names: {name: {"ActiveState": "active"} for name in names},
    )

    assert data["title"] == "Kalshi BTC 1s Paper Status"
    assert data["api_path"] in {"/api/dashboard", "/api/status"}
    assert data["boundary"] == (
        "Read-only dashboard. No live orders. Active system is 1s recorder + 1s paper executor."
    )
    metrics = data["paper_performance"]["metrics"]
    assert metrics["realized_pnl"] == pytest.approx(20.4545)
    assert metrics["open_positions"] == 1
    assert metrics["closed_positions"] == 1
    assert data["paper_performance"]["review_trades"]
    assert data["paper_performance"]["review_buckets"]["side"]
    assert data["top_blockers"][0]["blocker"] == "max_open_positions"

    html = dashboard.render_status_dashboard_html(data)

    assert "Kalshi BTC 1s Paper Status" in html
    assert "Paper PnL Review" in html
    assert "Paper Review Buckets" in html
    assert "Recent paper trades" in html
    assert "Grouped by signal" in html
    assert "Grouped by market" in html
    assert "Top blockers" in html
    assert "kalshi-btc15m-1s-recorder.service" in html
    assert "kalshi-btc15m-1s-paper.service" in html
    for term in BANNED_DASHBOARD_HTML_TERMS:
        assert term not in html


def test_active_dashboard_scripts_and_deploy_service_are_read_only() -> None:
    root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    scripts = pyproject["project"]["scripts"]

    assert scripts["kbtc15-1s-dashboard"] == "kalshibtc.dashboard:main"
    assert scripts["kbtc15-1s-stream-dashboard"] == "kalshibtc.dashboard:main_stream"
    assert scripts["kbtc15-1s-status-dashboard"] == "kalshibtc.dashboard:main_status"


def test_dashboard_help_commands_work() -> None:
    root = Path(__file__).resolve().parents[1]
    module_help = subprocess.run(
        [sys.executable, "-m", "kalshibtc.dashboard", "--help"],
        cwd=root,
        check=False,
        text=True,
        capture_output=True,
    )
    assert module_help.returncode == 0
    assert "kbtc15-1s-dashboard" in module_help.stdout
    assert "--snapshot-db" in module_help.stdout
    assert "--results-db" in module_help.stdout

    script_help = subprocess.run(
        ["uv", "run", "kbtc15-1s-dashboard", "--help"],
        cwd=root,
        check=False,
        text=True,
        capture_output=True,
    )
    assert script_help.returncode == 0
    assert "kbtc15-1s-dashboard" in script_help.stdout


def test_active_dashboard_deploy_service_is_read_only() -> None:
    root = Path(__file__).resolve().parents[1]
    service = root / "deploy" / "kalshi-btc15m-1s-dashboard.service"
    text = service.read_text()
    assert "kbtc15-1s-dashboard" in text
    assert "runtime/snapshots/realtime-snapshots-1s.sqlite3" in text
    assert "runtime/results/paper-results-1s.sqlite3" in text
    assert "EnvironmentFile" not in text
    for term in BANNED_DASHBOARD_HTML_TERMS:
        assert term not in text


def test_active_kalshibtc_source_does_not_import_legacy_package() -> None:
    package_root = Path(__file__).resolve().parents[1] / "src" / "kalshibtc"
    offenders: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "kalshi_btc_15m_bot" or alias.name.startswith(
                        "kalshi_btc_15m_bot."
                    ):
                        offenders.append(f"{path.relative_to(package_root)} imports {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "kalshi_btc_15m_bot" or module.startswith("kalshi_btc_15m_bot."):
                    offenders.append(f"{path.relative_to(package_root)} imports {module}")

    assert offenders == []
