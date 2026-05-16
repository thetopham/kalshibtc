from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from kalshibtc import dashboard
from kalshibtc.reports.volatility_hedge_summary import summarize


def test_volatility_hedge_detail_omits_large_raw_json_and_trims_nested_position_json(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "live" / "volatility_hedge"
    run_dir.mkdir(parents=True)
    (run_dir / "config.toml").write_text('mode = "live_paper"\nstrategy = "volatility_hedge"\n', encoding="utf-8")
    (run_dir / "metrics.json").write_text(
        json.dumps({"mode": "live_paper", "strategy": "volatility_hedge", "run_id": "live", "snapshots": 1, "signals": 1, "fills": 2}),
        encoding="utf-8",
    )
    with sqlite3.connect(run_dir / "results.sqlite3") as conn:
        conn.execute(
            """
            CREATE TABLE volatility_hedge_positions (
                event_key TEXT PRIMARY KEY, market_ticker TEXT, market_close_time TEXT, strike REAL,
                up_qty REAL, down_qty REAL, up_avg_entry REAL, down_avg_entry REAL,
                paired_qty REAL, paired_cost REAL, edge REAL, locked_payout REAL,
                locked_edge_dollars REAL, imbalance_ratio REAL, total_cost REAL,
                last_features_json TEXT, fills_json TEXT, updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE volatility_hedge_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT, market_ticker TEXT, ts TEXT,
                side TEXT, event_type TEXT, reason TEXT, price REAL, qty REAL,
                projected_paired_cost REAL, current_paired_cost REAL, edge REAL, up_qty REAL,
                down_qty REAL, time_to_expiry REAL, slope REAL, atr REAL, distance_from_strike REAL,
                up_bid REAL, up_ask REAL, down_bid REAL, down_ask REAL, raw_json TEXT
            )
            """
        )
        fills = [{"n": i, "blob": "x" * 1000} for i in range(25)]
        conn.execute(
            """
            INSERT INTO volatility_hedge_positions VALUES (
                'event', 'ticker', 'close', 100000, 10, 10, 0.45, 0.50, 10, 0.95,
                0.05, 10, 0.5, 1.0, 9.5, '{}', ?, '2026-05-16T00:00:00+00:00'
            )
            """,
            (json.dumps(fills),),
        )
        for idx in range(3):
            conn.execute(
                """
                INSERT INTO volatility_hedge_events (
                    event_key, market_ticker, ts, side, event_type, reason, price, qty,
                    projected_paired_cost, current_paired_cost, edge, up_qty, down_qty,
                    time_to_expiry, slope, atr, distance_from_strike, up_bid, up_ask,
                    down_bid, down_ask, raw_json
                ) VALUES ('event', 'ticker', ?, 'UP', 'decision', 'reason', 0.1, 1,
                    0.95, 0.96, 0.04, 10, 10, 100, 1, 2, 3, 0.1, 0.2, 0.7, 0.8, ?)
                """,
                (f"2026-05-16T00:00:0{idx}+00:00", json.dumps({"payload": "y" * 50000})),
            )

    detail = dashboard.collect_strategy_run_detail_data(runs_dir=runs_dir, strategy="volatility_hedge", run_id="live")

    assert len(detail["volatility_hedge_events"]) == 3
    assert "raw_json" not in detail["volatility_hedge_events"][0]
    position = detail["volatility_hedge_positions"][0]
    assert position["fills_json_count"] == 25
    assert len(json.loads(position["fills_json"])) == 10
    html = dashboard.render_strategy_run_detail_html(detail)
    assert "VolatilityHedgeStrategy paired cost" in html


def test_volatility_hedge_phase_counts_in_report_and_dashboard(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "live" / "volatility_hedge"
    run_dir.mkdir(parents=True)
    (run_dir / "config.toml").write_text('mode = "live_paper"\nstrategy = "volatility_hedge"\n', encoding="utf-8")
    (run_dir / "metrics.json").write_text(
        json.dumps({"mode": "live_paper", "strategy": "volatility_hedge", "run_id": "live", "snapshots": 3, "signals": 3, "fills": 1}),
        encoding="utf-8",
    )
    db = run_dir / "results.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE volatility_hedge_positions (
                event_key TEXT PRIMARY KEY, market_ticker TEXT, market_close_time TEXT, strike REAL,
                up_qty REAL, down_qty REAL, up_avg_entry REAL, down_avg_entry REAL,
                paired_qty REAL, paired_cost REAL, edge REAL, locked_payout REAL,
                locked_edge_dollars REAL, imbalance_ratio REAL, total_cost REAL,
                mode TEXT, settlement_EV REAL, worst_case_pnl REAL, repair_qty_needed REAL,
                residual_up_qty REAL, residual_down_qty REAL, lifecycle_phase TEXT,
                last_features_json TEXT, fills_json TEXT, updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE volatility_hedge_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT, market_ticker TEXT, ts TEXT,
                side TEXT, event_type TEXT, reason TEXT, price REAL, qty REAL,
                projected_paired_cost REAL, current_paired_cost REAL, edge REAL, up_qty REAL,
                down_qty REAL, time_to_expiry REAL, slope REAL, atr REAL, distance_from_strike REAL,
                up_bid REAL, up_ask REAL, down_bid REAL, down_ask REAL, lifecycle_phase TEXT,
                elapsed_seconds REAL, raw_json TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO volatility_hedge_positions VALUES (
                'event', 'ticker', 'close', 100000, 10, 10, 0.45, 0.50, 10, 0.95,
                0.05, 10, 0.5, 1.0, 9.5, 'NORMAL', 0.2, -0.5, 0, 0, 0,
                'MAIN_HARVEST', '{}', '[]', '2026-05-16T00:00:00+00:00'
            )
            """
        )
        rows = [
            ("EARLY_SEED", "decision", "early_seed_pair_cost_too_high", 0),
            ("MAIN_HARVEST", "fill", "initial_3_to_2_hedge", 100),
            ("MAIN_HARVEST", "decision", "main_seed_pair_cost_too_high", 200),
        ]
        for phase, event_type, reason, elapsed in rows:
            conn.execute(
                """
                INSERT INTO volatility_hedge_events (
                    event_key, market_ticker, ts, side, event_type, reason, price, qty,
                    projected_paired_cost, current_paired_cost, edge, up_qty, down_qty,
                    time_to_expiry, slope, atr, distance_from_strike, up_bid, up_ask,
                    down_bid, down_ask, lifecycle_phase, elapsed_seconds, raw_json
                ) VALUES ('event', 'ticker', ?, 'UP', ?, ?, 0.1, 1,
                    0.95, 0.96, 0.04, 10, 10, 100, 1, 2, 3, 0.1, 0.2, 0.7, 0.8, ?, ?, '{}')
                """,
                (f"2026-05-16T00:00:{elapsed // 100}{elapsed % 100}+00:00", event_type, reason, phase, elapsed),
            )

    summary = summarize(db)
    assert summary["phase_counts"] == {"EARLY_SEED": 1, "MAIN_HARVEST": 2}
    assert summary["fills_by_phase"] == {"MAIN_HARVEST": 1}
    assert summary["rejections_by_phase"]["MAIN_HARVEST"] == 1
    assert summary["current_phase_by_market"] == {"ticker": "MAIN_HARVEST"}

    detail = dashboard.collect_strategy_run_detail_data(runs_dir=runs_dir, strategy="volatility_hedge", run_id="live")
    assert detail["volatility_hedge_phase_counts"] == {"EARLY_SEED": 1, "MAIN_HARVEST": 2}
    assert detail["volatility_hedge_fills_by_phase"] == {"MAIN_HARVEST": 1}
    assert detail["volatility_hedge_rejections_by_phase"]["EARLY_SEED"] == 1
    assert detail["volatility_hedge_current_phase_by_market"] == {"ticker": "MAIN_HARVEST"}
    html = dashboard.render_strategy_run_detail_html(detail)
    assert "VolatilityHedge lifecycle phases" in html
    assert "MAIN_HARVEST" in html
