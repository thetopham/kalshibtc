from __future__ import annotations

import gc
import json
import sqlite3
from pathlib import Path

from kalshibtc import dashboard


def _make_run(run_dir: Path, idx: int) -> None:
    run_dir.mkdir(parents=True)
    (run_dir / "config.toml").write_text(f'strategy = "simple_directional"\nrun_id = "run-{idx}"\n')
    (run_dir / "metrics.json").write_text(
        json.dumps(
            {
                "strategy": "simple_directional",
                "run_id": f"run-{idx}",
                "snapshots": 1,
                "signals": 1,
                "fills": 0,
            }
        )
    )
    with sqlite3.connect(run_dir / "results.sqlite3") as conn:
        conn.execute(
            "CREATE TABLE replay_signals (id INTEGER PRIMARY KEY, ts TEXT, strategy TEXT, side TEXT, confidence REAL, reason TEXT, allowed INTEGER, blocked_by_json TEXT, raw_json TEXT)"
        )
        conn.execute("INSERT INTO replay_signals (ts, strategy, side, confidence, reason, allowed, blocked_by_json, raw_json) VALUES ('2026-05-15T12:00:00+00:00', 'simple_directional', 'none', 0.0, 'x', 0, '[\"signal_none\"]', '{}')")


def test_strategy_run_scanner_does_not_leave_results_db_file_descriptors_open(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    for i in range(8):
        _make_run(runs_dir / "simple_directional" / f"run-{i}", i)

    gc.collect()
    before = _fd_targets(tmp_path)
    data = dashboard.collect_strategy_runs_dashboard_data(runs_dir=runs_dir)
    gc.collect()
    after = _fd_targets(tmp_path)

    assert len(data["runs"]) == 8
    assert before == after == []


def test_strategy_run_scanner_bounds_recent_runs_per_strategy(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    for i in range(8):
        _make_run(runs_dir / "simple_directional" / f"run-{i:02d}", i)

    data = dashboard.collect_strategy_runs_dashboard_data(
        runs_dir=runs_dir,
        max_runs_per_strategy=3,
        max_total_runs=10,
    )

    assert len(data["runs"]) == 3
    assert [Path(str(run["run_dir"])).name for run in data["runs"]] == ["run-07", "run-06", "run-05"]
    assert data["max_runs_per_strategy"] == 3
    assert data["max_total_runs"] == 10


def test_result_blockers_reads_recent_rows_without_leaking_file_descriptors(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "simple_directional" / "run-a"
    _make_run(run_dir, 1)

    gc.collect()
    before = _fd_targets(tmp_path)
    blockers = dashboard._result_blockers(run_dir / "results.sqlite3")
    gc.collect()
    after = _fd_targets(tmp_path)

    assert blockers == {"signal_none": 1}
    assert before == after == []


def test_read_result_rows_closes_results_db_file_descriptor(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / "simple_directional" / "run-a"
    _make_run(run_dir, 1)

    gc.collect()
    before = _fd_targets(tmp_path)
    rows = dashboard._read_result_rows(run_dir / "results.sqlite3", "replay_signals", limit=10)
    gc.collect()
    after = _fd_targets(tmp_path)

    assert len(rows) == 1
    assert before == after == []


def _fd_targets(root: Path) -> list[str]:
    fd_root = Path("/proc/self/fd")
    targets: list[str] = []
    if not fd_root.exists():
        return targets
    for fd in fd_root.iterdir():
        try:
            target = fd.resolve(strict=True)
        except OSError:
            continue
        if str(target).startswith(str(root)):
            targets.append(str(target))
    return sorted(targets)
