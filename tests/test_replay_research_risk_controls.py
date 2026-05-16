from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from test_feed_replay_architecture import _write_feed_db

from kalshibtc.replay.cli import main as replay_main


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
    assert fills >= 1
