from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from kalshibtc.probability.dataset import export_probability_dataset, load_probability_rows
from kalshibtc.probability.dataset_cli import main as probability_dataset_main
from kalshibtc.probability.evaluation import (
    brier_score,
    calibration_report,
    ev_capture_metrics,
    expected_calibration_error,
    log_loss,
    walk_forward_splits,
)


def _write_probability_feed_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
        rows = [
            ("m1", "2026-05-18T08:00:00+00:00", "2026-05-18T08:00:00+00:00", "2026-05-18T08:15:00+00:00", 99_980.0, 100_000.0, 0.44, 0.46, 0.53, 0.55, {"atr_60s": 20, "vwap": 99_970, "wickiness": 0.2}),
            ("m1", "2026-05-18T08:05:00+00:00", "2026-05-18T08:00:00+00:00", "2026-05-18T08:15:00+00:00", 100_010.0, 100_000.0, 0.51, 0.53, 0.46, 0.48, {"atr_60s": 18, "vwap": 100_000, "range_expansion": 1.1}),
            ("m1", "2026-05-18T08:14:59+00:00", "2026-05-18T08:00:00+00:00", "2026-05-18T08:15:00+00:00", 100_030.0, 100_000.0, 0.96, 0.98, 0.01, 0.02, {"atr_60s": 12}),
            ("m2", "2026-05-18T08:15:00+00:00", "2026-05-18T08:15:00+00:00", "2026-05-18T08:30:00+00:00", 100_050.0, 100_040.0, 0.54, 0.56, 0.43, 0.45, {"atr_60s": 16, "wickiness": 0.8}),
            ("m2", "2026-05-18T08:20:00+00:00", "2026-05-18T08:15:00+00:00", "2026-05-18T08:30:00+00:00", 100_010.0, 100_040.0, 0.31, 0.33, 0.66, 0.68, {"atr_60s": 22, "atr_slope": 0.6}),
            ("m2", "2026-05-18T08:29:59+00:00", "2026-05-18T08:15:00+00:00", "2026-05-18T08:30:00+00:00", 100_000.0, 100_040.0, 0.02, 0.03, 0.95, 0.97, {"atr_60s": 15}),
            ("m3", "2026-05-18T08:30:00+00:00", "2026-05-18T08:30:00+00:00", "2026-05-18T08:45:00+00:00", 100_000.0, 99_990.0, 0.57, 0.59, 0.40, 0.42, {"atr_60s": 14, "ema_slope": 2.0}),
            ("m3", "2026-05-18T08:44:59+00:00", "2026-05-18T08:30:00+00:00", "2026-05-18T08:45:00+00:00", 99_970.0, 99_990.0, 0.01, 0.02, 0.95, 0.97, {"atr_60s": 14, "ema_slope": -2.0}),
        ]
        for seq, (ticker, ts, open_time, close_time, price, strike, yes_bid, yes_ask, no_bid, no_ask, raw) in enumerate(rows, start=1):
            conn.execute(
                """
                INSERT INTO realtime_snapshots_1s (
                    ts, market_ticker, market_open_time, market_close_time, btc_price,
                    strike, target_price, distance_from_strike, seconds_to_close,
                    btc_velocity_30s, slope_30s, yes_bid, yes_ask, no_bid, no_ask,
                    orderbook_sequence, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    ticker,
                    open_time,
                    close_time,
                    price,
                    strike,
                    strike,
                    price - strike,
                    0.0,
                    raw.get("ema_slope", 0.0),
                    raw.get("ema_slope", 0.0),
                    yes_bid,
                    yes_ask,
                    no_bid,
                    no_ask,
                    seq,
                    json.dumps(raw),
                ),
            )


def test_probability_dataset_export_labels_snapshots_and_writes_csv(tmp_path: Path) -> None:
    feed_db = tmp_path / "feed.sqlite3"
    out = tmp_path / "probability.csv"
    _write_probability_feed_db(feed_db)

    summary = export_probability_dataset(
        feed_db=feed_db,
        venue="polymarket",
        from_ts="2026-05-18T08:00:00+00:00",
        to_ts="2026-05-18T08:45:00+00:00",
        output_path=out,
    )

    assert summary.rows == 8
    assert summary.markets == 3
    assert summary.output_path == out
    rows = list(csv.DictReader(out.open()))
    assert rows[0]["venue"] == "polymarket"
    assert rows[0]["market_ticker"] == "m1"
    assert rows[0]["final_outcome"] == "yes"
    assert float(rows[0]["realized_move_into_close"]) == pytest.approx(50.0)
    assert float(rows[0]["market_implied_probability"]) == pytest.approx(0.46)
    assert rows[0]["split_key"] == "train"
    assert rows[-1]["split_key"] == "test"
    assert {"trend", "chop", "volatility_expansion", "low_liquidity"} >= {row["regime_label"] for row in rows}
    assert "z_score" in rows[0]
    assert "distance_from_vwap" in rows[0]


def test_probability_dataset_cli_exports_csv(tmp_path: Path) -> None:
    feed_db = tmp_path / "feed.sqlite3"
    out = tmp_path / "dataset.csv"
    _write_probability_feed_db(feed_db)

    exit_code = probability_dataset_main(
        [
            "--feed-db",
            str(feed_db),
            "--venue",
            "kalshi",
            "--from",
            "2026-05-18T08:00:00+00:00",
            "--to",
            "2026-05-18T08:45:00+00:00",
            "--output",
            str(out),
            "--format",
            "csv",
        ]
    )

    assert exit_code == 0
    rows = load_probability_rows(out)
    assert len(rows) == 8
    assert rows[0]["venue"] == "kalshi"


def test_calibration_metrics_score_probability_quality_and_buckets() -> None:
    probs = [0.1, 0.2, 0.8, 0.9]
    labels = [False, False, True, True]

    assert brier_score(probs, labels) == pytest.approx(0.025)
    assert log_loss(probs, labels) < 0.2
    report = calibration_report(probs, labels, bins=2)
    assert report.buckets[0].count == 2
    assert report.buckets[0].observed_frequency == 0.0
    assert report.buckets[1].observed_frequency == 1.0
    assert expected_calibration_error(probs, labels, bins=2) == pytest.approx(0.15)


def test_walk_forward_splits_are_chronological_by_market() -> None:
    rows = [
        {"market_ticker": "m1", "ts": "2026-05-18T08:00:00+00:00"},
        {"market_ticker": "m2", "ts": "2026-05-18T08:15:00+00:00"},
        {"market_ticker": "m3", "ts": "2026-05-18T08:30:00+00:00"},
        {"market_ticker": "m4", "ts": "2026-05-18T08:45:00+00:00"},
    ]

    splits = walk_forward_splits(rows, train_markets=2, test_markets=1)

    assert len(splits) == 2
    assert [row["market_ticker"] for row in splits[0].train] == ["m1", "m2"]
    assert [row["market_ticker"] for row in splits[0].test] == ["m3"]
    assert [row["market_ticker"] for row in splits[1].train] == ["m2", "m3"]
    assert [row["market_ticker"] for row in splits[1].test] == ["m4"]


def test_ev_capture_metrics_use_only_ev_filtered_trades() -> None:
    rows = [
        {"model_probability": 0.7, "market_implied_probability": 0.55, "final_outcome_yes": True},
        {"model_probability": 0.6, "market_implied_probability": 0.58, "final_outcome_yes": False},
        {"model_probability": 0.3, "market_implied_probability": 0.45, "final_outcome_yes": False},
    ]

    metrics = ev_capture_metrics(rows, edge_threshold=0.05, fee_rate=0.0, slippage=0.0)

    assert metrics["trades"] == 2
    assert metrics["wins"] == 2
    assert metrics["ev_capture"] > 0
    assert metrics["sharpe"] > 0
