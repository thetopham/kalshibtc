from __future__ import annotations

import importlib.util
import json
import sqlite3
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from kalshibtc.replay.cli import _rows_to_replay_inputs
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
        "kbtc-feed-derived-columns": "kalshibtc.feed_derived_columns:main",
        "kbtc-replay": "kalshibtc.replay.cli:main",
        "kbtc-dashboard": "kalshibtc.dashboard:main",
        "kbtc-probability-dataset": "kalshibtc.probability.dataset_cli:main",
        "kbtc-paper": "kalshibtc.paper_signal_executor:main",
        "kbtc-research-journal": "kalshibtc.research.journal:main",
        "kbtc-research": "kalshibtc.research.runner:main",
        "kbtc-auto-strategy-creator": "kalshibtc.research.auto_strategy_creator:main",
        "kbtc-poly-fill-validate": "kalshibtc.polymarket_fill_validation:main",
        "polymarket-btc-15m-recorder": "kalshibtc.polymarket_btc_15m_recorder:main",
    }


def test_legacy_15m_package_is_archive_only_not_active_import_path() -> None:
    assert importlib.util.find_spec("kalshi_btc_15m_bot") is None
    assert Path("archive/legacy-15m/src/kalshi_btc_15m_bot/streaming.py").is_file()


def test_rows_to_replay_inputs_preserves_top_level_market_metadata_when_raw_json_is_nested(tmp_path: Path) -> None:
    feed_db = tmp_path / "poly-shape.sqlite3"
    with sqlite3.connect(feed_db) as conn:
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
        nested_raw = json.dumps({"snapshot": {"strike": 15.0, "market_close_time": "2099-01-01T00:00:00+00:00"}})
        conn.execute(
            """
            INSERT INTO realtime_snapshots_1s (
                ts, market_ticker, market_open_time, market_close_time, btc_price, strike,
                target_price, yes_bid, yes_ask, no_bid, no_ask, orderbook_sequence, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-05-18T08:45:00+00:00",
                "btc-updown-15m-1779093900",
                "2026-05-18T08:45:00+00:00",
                "2026-05-18T09:00:00+00:00",
                77_000.0,
                76_900.0,
                76_900.0,
                0.49,
                0.50,
                0.49,
                0.50,
                1,
                nested_raw,
            ),
        )
        conn.row_factory = sqlite3.Row
        rows = list(conn.execute("SELECT * FROM realtime_snapshots_1s"))

    _, books, contract, _ = _rows_to_replay_inputs(rows)

    assert contract.strike == 76_900.0
    assert contract.close_time == datetime(2026, 5, 18, 9, 0, tzinfo=UTC)
    assert books[0].raw["strike"] == 76_900.0
    assert books[0].raw["market_close_time"] == "2026-05-18T09:00:00+00:00"
    assert books[0].raw["snapshot"]["strike"] == 15.0


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


def test_replay_cli_can_enable_composite_reference_without_default_strategy_change(tmp_path: Path) -> None:
    feed_db = tmp_path / "feed" / "kalshi-btc-1s.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_feed_db(feed_db)
    with sqlite3.connect(feed_db) as conn:
        conn.execute(
            """
            UPDATE realtime_snapshots_1s
            SET raw_json = ?
            WHERE ts = '2026-05-15T12:00:30+00:00'
            """,
            (
                json.dumps(
                    {
                        "btc_venue_observations": [
                            {"venue": "coinbase", "ts": "2026-05-15T12:00:30+00:00", "price": 99_990.0},
                            {"venue": "kraken", "ts": "2026-05-15T12:00:30+00:00", "price": 100_000.0},
                            {"venue": "bitstamp", "ts": "2026-05-15T12:00:30+00:00", "price": 100_010.0},
                        ]
                    }
                ),
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
            "raw-default",
            "--json",
        ]
    ) == 0
    assert replay_main(
        [
            "--feed-db",
            str(feed_db),
            "--runs-dir",
            str(runs_dir),
            "--strategy",
            "simple_directional",
            "--run-id",
            "composite-enabled",
            "--reference-price-source",
            "composite_60s_reference",
            "--json",
        ]
    ) == 0

    raw_metrics = json.loads((runs_dir / "simple_directional" / "raw-default" / "metrics.json").read_text())
    composite_metrics = json.loads(
        (runs_dir / "simple_directional" / "composite-enabled" / "metrics.json").read_text()
    )
    assert raw_metrics["reference_price_source"] == "single_venue"
    assert composite_metrics["reference_price_source"] == "composite_60s_reference"
    assert composite_metrics["reference_price_provenance"]["single_venue_ticks"] == 2
    assert composite_metrics["reference_price_provenance"]["composite_60s_reference_ticks"] == 1
    assert composite_metrics["reference_price_provenance"]["warnings"] == []

    with sqlite3.connect(runs_dir / "simple_directional" / "composite-enabled" / "results.sqlite3") as conn:
        raw = json.loads(conn.execute("SELECT raw_json FROM replay_signals WHERE raw_json LIKE '%composite_60s_reference%' LIMIT 1").fetchone()[0])

    assert raw["state"]["reference_price_source"] == "composite_60s_reference"
    assert raw["state"]["reference_price"] == 100_000.0
    assert raw["state"]["raw_btc_price"] == 100_025.0


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
