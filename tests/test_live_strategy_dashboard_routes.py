from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from kalshibtc import dashboard


def test_live_strategy_detail_route_maps_to_runs_live_strategy_slot(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    live_dir = runs_dir / "live" / "spread_aware_momentum"
    live_dir.mkdir(parents=True)
    (live_dir / "config.toml").write_text('mode = "live_paper"\nstrategy = "spread_aware_momentum"\n')
    (live_dir / "metrics.json").write_text(
        json.dumps(
            {
                "mode": "live_paper",
                "strategy": "spread_aware_momentum",
                "run_id": "live",
                "snapshots": 1,
                "signals": 1,
                "fills": 0,
            }
        )
    )
    with sqlite3.connect(live_dir / "results.sqlite3") as conn:
        conn.execute(
            "CREATE TABLE predictions (id TEXT PRIMARY KEY, created_at TEXT, strategy TEXT, action TEXT, side TEXT, confidence REAL, reasons_json TEXT)"
        )
        conn.execute(
            "CREATE TABLE paper_trades (id TEXT PRIMARY KEY, created_at TEXT, strategy TEXT, side TEXT, status TEXT, notional REAL)"
        )
        conn.execute(
            "INSERT INTO predictions (id, created_at, strategy, action, side, confidence, reasons_json) VALUES ('p1', '2026-05-15T12:00:00+00:00', 'spread_aware_momentum', 'NO_TRADE', NULL, 0.0, '[]')"
        )

    data = dashboard.collect_strategy_run_detail_data(
        runs_dir=runs_dir,
        strategy="spread_aware_momentum",
        run_id="live",
    )

    assert data["run"]["run_dir"] == str(live_dir)
    assert data["run"]["strategy"] == "spread_aware_momentum"
    assert data["run"]["run_id"] == "live"
    assert data["signals"] == []
    assert data["fills"] == []
