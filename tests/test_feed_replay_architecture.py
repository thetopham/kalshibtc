from __future__ import annotations

import importlib.util
import json
import sqlite3
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from kalshibtc.replay.cli import main as replay_main
from kalshibtc.runtime_paths import (
    DEFAULT_FEED_DB,
    DEFAULT_RUNS_DIR,
    resolve_feed_db,
    resolve_runs_dir,
)


def _write_feed_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    close = datetime(2026, 5, 15, 12, 15, tzinfo=UTC).isoformat()
    open_time = datetime(2026, 5, 15, 12, 0, tzinfo=UTC).isoformat()
    rows = [
        ("2026-05-15T12:00:00+00:00", 99_950.0, -1.0, 0.47, 0.49, 0.51, 0.53),
        ("2026-05-15T12:00:30+00:00", 100_025.0, 2.0, 0.52, 0.54, 0.45, 0.47),
        ("2026-05-15T12:00:31+00:00", 100_050.0, 2.5, 0.53, 0.55, 0.44, 0.46),
    ]
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE realtime_snapshots_1s (
                ts TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                market_open_time TEXT,
                market_close_time TEXT NOT NULL,
                btc_price REAL NOT NULL,
                strike REAL NOT NULL,
                target_price REAL,
                distance_from_strike REAL,
                seconds_to_close REAL,
                btc_velocity_30s REAL,
                slope_30s REAL,
                yes_bid REAL,
                yes_ask REAL,
                no_bid REAL,
                no_ask REAL,
                orderbook_sequence INTEGER,
                raw_json TEXT,
                PRIMARY KEY (market_ticker, ts)
            )
            """
        )
        for index, (ts, price, slope, yes_bid, yes_ask, no_bid, no_ask) in enumerate(rows, start=1):
            conn.execute(
                """
                INSERT INTO realtime_snapshots_1s (
                    ts, market_ticker, market_open_time, market_close_time,
                    btc_price, strike, target_price, distance_from_strike,
                    seconds_to_close, btc_velocity_30s, slope_30s,
                    yes_bid, yes_ask, no_bid, no_ask, orderbook_sequence, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    "KXBTC15M-TEST",
                    open_time,
                    close,
                    price,
                    100_000.0,
                    100_000.0,
                    price - 100_000.0,
                    900.0 - index,
                    slope,
                    slope,
                    yes_bid,
                    yes_ask,
                    no_bid,
                    no_ask,
                    index,
                    "{}",
                ),
            )


def test_runtime_paths_use_feed_and_runs_without_legacy_fallback(tmp_path: Path) -> None:
    assert DEFAULT_FEED_DB == Path("feed/kalshi-btc-1s.sqlite3")
    assert DEFAULT_RUNS_DIR == Path("runs")
    assert resolve_feed_db(None, env={}) == DEFAULT_FEED_DB
    assert resolve_runs_dir(None, env={}) == DEFAULT_RUNS_DIR
    assert resolve_feed_db(tmp_path / "custom.sqlite3") == tmp_path / "custom.sqlite3"
    assert resolve_runs_dir(tmp_path / "custom-runs") == tmp_path / "custom-runs"


def test_console_scripts_expose_feed_replay_dashboard_without_legacy_kbtc15() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())

    assert pyproject["project"]["scripts"] == {
        "kbtc-feed": "kalshibtc.record_1s_snapshots:main",
        "kbtc-replay": "kalshibtc.replay.cli:main",
        "kbtc-dashboard": "kalshibtc.dashboard:main",
        "kbtc-paper": "kalshibtc.paper_signal_executor:main",
    }


def test_legacy_15m_package_is_archive_only_not_active_import_path() -> None:
    assert importlib.util.find_spec("kalshi_btc_15m_bot") is None
    assert Path("archive/legacy-15m/src/kalshi_btc_15m_bot/streaming.py").is_file()


def test_replay_cli_reads_feed_db_and_writes_immutable_run_outputs(tmp_path: Path) -> None:
    feed_db = tmp_path / "feed" / "kalshi-btc-1s.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_feed_db(feed_db)

    exit_code = replay_main(
        [
            "--feed-db",
            str(feed_db),
            "--runs-dir",
            str(runs_dir),
            "--strategy",
            "simple_directional",
            "--run-id",
            "unit-run",
            "--json",
        ]
    )

    assert exit_code == 0
    run_dir = runs_dir / "simple_directional" / "unit-run"
    assert (run_dir / "config.toml").is_file()
    assert (run_dir / "metrics.json").is_file()
    assert (run_dir / "results.sqlite3").is_file()
    metrics = json.loads((run_dir / "metrics.json").read_text())
    assert metrics["snapshots"] == 3
    assert metrics["signals"] >= 1
    assert metrics["fills"] >= 1

    with sqlite3.connect(run_dir / "results.sqlite3") as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        fills = conn.execute("SELECT COUNT(*) FROM replay_fills").fetchone()[0]

    assert {"replay_signals", "replay_fills"} <= tables
    assert fills >= 1


def test_replay_cli_refuses_to_overwrite_existing_run(tmp_path: Path) -> None:
    feed_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_feed_db(feed_db)

    args = [
        "--feed-db",
        str(feed_db),
        "--runs-dir",
        str(runs_dir),
        "--strategy",
        "simple_directional",
        "--run-id",
        "same-run",
    ]
    assert replay_main(args) == 0
    assert replay_main(args) == 2
