from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from kalshibtc import dashboard
from kalshibtc.strategy.dynamic_complement_hedge import DynamicHedgeConfig, replay_feed_db
from test_dynamic_complement_hedge import _write_dynamic_feed_db

sys.path.append(str(Path(__file__).resolve().parents[1] / "scripts"))
from replay_dynamic_hedge import main as dynamic_main  # noqa: E402


def test_temporal_basis_compression_scan_only_records_best_basis_without_trades(tmp_path: Path) -> None:
    feed_db = tmp_path / "feed.sqlite3"
    out_dir = tmp_path / "scan"
    _write_dynamic_feed_db(feed_db)

    summary = replay_feed_db(
        feed_db,
        out_dir=out_dir,
        config=DynamicHedgeConfig(slippage=0.0),
        scan_only=True,
    )

    assert summary["mode"] == "scan_only"
    assert summary["markets_tested"] == 1
    assert summary["markets_traded"] == 0
    assert summary["fills"] == 0
    assert summary["temporal_basis_compression"]["count_best_combined_basis_lt"]["0.85"] == 1
    assert summary["temporal_basis_compression"]["best_basis_min"] == 0.63
    assert (out_dir / "temporal_basis_compression.csv").read_text().splitlines()[1].split(",")[0] == "A"
    observations = (out_dir / "temporal_observations.csv").read_text()
    assert "temporal_edge" in observations
    assert "seconds_to_close" in observations
    assert "abs_distance_from_strike" in observations

    with sqlite3.connect(feed_db) as conn:
        assert {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"realtime_snapshots_1s"}


def test_dynamic_replay_script_supports_scan_only_mode(tmp_path: Path) -> None:
    feed_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_dynamic_feed_db(feed_db)

    assert dynamic_main(
        [
            "--feed-db",
            str(feed_db),
            "--runs-dir",
            str(runs_dir),
            "--run-id",
            "unit",
            "--scan-only",
            "--slippage",
            "0",
            "--json",
        ]
    ) == 0

    metrics_path = runs_dir / "dynamic_complement_hedge" / "temporal_scan-unit" / "metrics.json"
    assert metrics_path.is_file()
    assert '"mode": "scan_only"' in metrics_path.read_text()


def test_stream_dashboard_exposes_temporal_basis_compression_panel(tmp_path: Path) -> None:
    feed_db = tmp_path / "feed.sqlite3"
    _write_dynamic_feed_db(feed_db)

    data = dashboard.collect_stream_dashboard_data(snapshot_db=feed_db, history_limit=20)
    temporal = data["stream"]["temporal_basis_compression"]

    assert temporal["source_rows"] == 5
    assert temporal["count_best_combined_basis_lt"]["0.85"] == 1
    assert temporal["rows"][0]["best_combined_basis_seen"] == 0.63
    assert temporal["rows"][0]["time_to_expiry_at_best"] == 1.0
    assert temporal["rows"][0]["distance_from_strike_at_best"] == 110.0

    html = dashboard.render_stream_dashboard_html(data)
    assert "Temporal Basis Compression" in html
    assert "id=\"temporal-compression-table\"" in html
    assert "id=\"temporal-compression-chart\"" in html
