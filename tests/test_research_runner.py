from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from kalshibtc.research.runner import main


def _write_minimal_feed_db(path: Path) -> None:
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
                yes_bid REAL,
                yes_ask REAL,
                no_bid REAL,
                no_ask REAL,
                raw_json TEXT
            )
            """
        )
        rows = [
            ("2026-05-22T12:14:00Z", 50020.0),
            ("2026-05-22T12:14:30Z", 50025.0),
            ("2026-05-22T12:14:45Z", 50030.0),
        ]
        for ts, price in rows:
            conn.execute(
                """
                INSERT INTO realtime_snapshots_1s (
                    ts, market_ticker, market_open_time, market_close_time, btc_price, strike,
                    yes_bid, yes_ask, no_bid, no_ask, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    "KXBTCD-26MAY221215-B50000",
                    "2026-05-22T12:00:00Z",
                    "2026-05-22T12:15:00Z",
                    price,
                    50000.0,
                    0.58,
                    0.60,
                    0.39,
                    0.41,
                    "{}",
                ),
            )


def _write_spec(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "name": "late_breakout_candidate",
                "economic_story": "Late BTC distance from strike may persist into settlement.",
                "mechanism": "parameterized_late_window",
                "parameters": {
                    "max_seconds_to_close": 90,
                    "min_distance": 10,
                    "max_entry_price": 0.65,
                    "max_entry_spread": 0.05,
                    "target_notional": 12.0,
                },
                "falsification": "Fails if replay cannot clear basic gates.",
                "created_at": "2026-05-22T12:00:00Z",
                "parent_id": "manual",
            }
        ),
        encoding="utf-8",
    )


def test_research_runner_writes_summary_and_run_outputs_without_mutating_feed(tmp_path: Path) -> None:
    feed_db = tmp_path / "feed.sqlite3"
    spec_path = tmp_path / "spec.json"
    runs_dir = tmp_path / "runs"
    research_dir = tmp_path / "research"
    _write_minimal_feed_db(feed_db)
    _write_spec(spec_path)
    before = feed_db.read_bytes()

    exit_code = main(
        [
            "run",
            "--spec",
            str(spec_path),
            "--feed-db",
            str(feed_db),
            "--runs-dir",
            str(runs_dir),
            "--research-dir",
            str(research_dir),
            "--run-id",
            "pytest-candidate",
            "--json",
        ]
    )

    assert exit_code == 0
    assert feed_db.read_bytes() == before
    run_dir = runs_dir / "candidate_late_breakout_candidate" / "pytest-candidate"
    assert (run_dir / "metrics.json").is_file()
    assert (run_dir / "results.sqlite3").is_file()
    summary = json.loads((run_dir / "research_summary.json").read_text(encoding="utf-8"))
    assert summary["candidate"]["name"] == "late_breakout_candidate"
    assert summary["run_dir"] == str(run_dir)
    assert summary["metrics"]["institutional_metrics"]["trades"] >= 0
    assert summary["gates"]["passed"] in {True, False}
    assert summary["safety_boundary"] == "replay/research-only; no live orders"
    manifest = research_dir / "candidate_runs" / "manifest.csv"
    assert manifest.is_file()
    assert "pytest-candidate" in manifest.read_text(encoding="utf-8")
