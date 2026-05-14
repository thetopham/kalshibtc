from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_btc_15m_bot.cli import build_parser
from kalshi_btc_15m_bot.recorder import RealtimeSnapshotRecorder, snapshot_row_from_payload


def sample_stream_payload(**overrides):
    now = datetime(2026, 1, 4, 0, 5, 12, 345678, tzinfo=UTC)
    payload = {
        "event": "market_state",
        "as_of": now.isoformat(),
        "market_ticker": "KXBTC15M-TEST-45",
        "market_open_time": (now - timedelta(minutes=5)).isoformat(),
        "market_close_time": (now + timedelta(minutes=10)).isoformat(),
        "market_expiration_time": (now + timedelta(minutes=15)).isoformat(),
        "seconds_to_close": 600.0,
        "current_price": 100_125.0,
        "target_price": 100_000.0,
        "distance_to_target": 125.0,
        "distance_to_target_pct": 0.00125,
        "btc_velocity_10s": 2.5,
        "btc_velocity_30s": 1.25,
        "btc_velocity_60s": 0.5,
        "distance_velocity_30s": 1.25,
        "distance_expanding": True,
        "strike_crossed_recently": False,
        "seconds_since_last_strike_cross": 90.0,
        "yes_bid": 0.61,
        "yes_ask": 0.64,
        "no_bid": 0.36,
        "no_ask": 0.39,
        "yes_bid_depth": 10.0,
        "yes_ask_depth": 6.0,
        "no_bid_depth": 6.0,
        "no_ask_depth": 10.0,
        "spread": 0.03,
        "top_book": {"yes": [[0.61, 10.0]], "no": [[0.36, 6.0]]},
        "orderbook_sequence": 1234,
        "orderbook_valid": True,
        "market_implied_yes": 0.625,
        "probability_yes": 0.74,
        "probability_no": 0.26,
        "probability_delta_30s": 0.08,
        "probability_delta_60s": 0.11,
        "model_probability_yes": 0.70,
        "edge_yes": 0.10,
        "edge_no": -0.10,
        "best_side": "YES",
        "cumulative_volume": 1200.0,
        "volume_delta_1s": 4.0,
        "volume_delta_10s": 20.0,
        "volume_delta_60s": 75.0,
        "recent_trade_count": 3,
        "last_trade_price": 0.64,
        "last_trade_side": "YES",
        "execution_decision": {
            "action": "BUY_YES",
            "side": "YES",
            "confidence": 0.72,
            "regime": "trend_hold",
            "reason": "aligned trend with positive edge",
            "blocked_by": [],
        },
    }
    payload.update(overrides)
    return payload


def rows(recorder: RealtimeSnapshotRecorder):
    with recorder.connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM realtime_snapshots_1s")]


def test_insert_one_1s_snapshot(tmp_path) -> None:
    recorder = RealtimeSnapshotRecorder(tmp_path / "snapshots.sqlite3")

    inserted = recorder.record_snapshot(sample_stream_payload())

    assert inserted is True
    stored = rows(recorder)
    assert len(stored) == 1
    row = stored[0]
    assert row["market_ticker"] == "KXBTC15M-TEST-45"
    assert row["ts"] == "2026-01-04T00:05:12+00:00"
    assert row["time_bucket"] == "middle"
    assert row["btc_price"] == pytest.approx(100_125.0)
    assert row["target_price"] == pytest.approx(100_000.0)
    assert row["raw_state_json"]


def test_duplicate_snapshot_for_same_market_and_second_updates_existing_row(tmp_path) -> None:
    recorder = RealtimeSnapshotRecorder(tmp_path / "snapshots.sqlite3")
    payload = sample_stream_payload()

    assert recorder.record_snapshot(payload) is True
    assert recorder.record_snapshot({**payload, "current_price": 100_200.0}) is True

    stored = rows(recorder)
    assert len(stored) == 1
    assert stored[0]["btc_price"] == pytest.approx(100_200.0)
    assert json.loads(stored[0]["raw_state_json"])["current_price"] == pytest.approx(100_200.0)


def test_missing_volume_fields_do_not_crash_and_store_todo(tmp_path) -> None:
    recorder = RealtimeSnapshotRecorder(tmp_path / "snapshots.sqlite3")
    payload = sample_stream_payload()
    for key in [
        "cumulative_volume",
        "volume_delta_1s",
        "volume_delta_10s",
        "volume_delta_60s",
        "recent_trade_count",
        "last_trade_price",
        "last_trade_side",
    ]:
        payload.pop(key)

    recorder.record_snapshot(payload)

    row = rows(recorder)[0]
    assert row["cumulative_volume"] is None
    assert row["volume_delta_60s"] is None
    assert "trade" in row["volume_todo"].lower()


def test_missing_optional_probability_fields_do_not_crash(tmp_path) -> None:
    recorder = RealtimeSnapshotRecorder(tmp_path / "snapshots.sqlite3")
    payload = sample_stream_payload()
    for key in [
        "market_implied_yes",
        "probability_delta_30s",
        "probability_delta_60s",
        "model_probability_yes",
        "edge_yes",
        "edge_no",
        "best_side",
    ]:
        payload.pop(key)

    recorder.record_snapshot(payload)

    row = rows(recorder)[0]
    assert row["market_implied_yes"] is None
    assert row["probability_delta_30s"] is None
    assert row["model_probability_yes"] is None


def test_snapshot_row_includes_manual_observation_fields() -> None:
    row = snapshot_row_from_payload(sample_stream_payload())

    assert row["distance_from_strike"] == pytest.approx(125.0)
    assert row["abs_distance_from_strike"] == pytest.approx(125.0)
    assert row["is_above_strike"] == 1
    assert row["seconds_to_close"] == pytest.approx(600.0)
    assert row["slope_30s"] == pytest.approx(1.25)
    assert row["yes_bid"] == pytest.approx(0.61)
    assert row["yes_ask"] == pytest.approx(0.64)
    assert row["no_bid"] == pytest.approx(0.36)
    assert row["no_ask"] == pytest.approx(0.39)
    assert row["spread"] == pytest.approx(0.03)
    assert row["yes_spread"] == pytest.approx(0.03)
    assert row["no_spread"] == pytest.approx(0.03)
    assert row["min_spread"] == pytest.approx(0.03)
    assert row["execution_action"] == "BUY_YES"
    assert row["execution_side"] == "YES"
    assert row["execution_confidence"] == pytest.approx(0.72)
    assert row["execution_regime"] == "trend_hold"
    assert json.loads(row["execution_blocked_by_json"]) == []


def test_zero_values_are_preserved_in_fallback_fields() -> None:
    payload = sample_stream_payload(
        slope_10s=0.0,
        btc_velocity_10s=3.0,
        slope_30s=0.0,
        btc_velocity_30s=4.0,
        slope_60s=0.0,
        btc_velocity_60s=5.0,
        edge_yes=0.0,
        probability_edge_yes=0.25,
        edge_no=0.0,
        probability_edge_no=0.25,
        best_side="",
        best_ev_side="NO",
        spread=None,
        yes_bid=0.0,
        yes_ask=0.0,
        no_bid=0.02,
        no_ask=0.05,
    )

    row = snapshot_row_from_payload(payload)

    assert row["slope_10s"] == pytest.approx(0.0)
    assert row["slope_30s"] == pytest.approx(0.0)
    assert row["slope_60s"] == pytest.approx(0.0)
    assert row["edge_yes"] == pytest.approx(0.0)
    assert row["edge_no"] == pytest.approx(0.0)
    assert row["best_side"] is None
    assert row["yes_spread"] == pytest.approx(0.0)
    assert row["min_spread"] == pytest.approx(0.0)
    assert row["spread"] == pytest.approx(0.0)


def test_yes_no_and_min_spreads_are_computed_from_top_of_book() -> None:
    row = snapshot_row_from_payload(
        sample_stream_payload(
            spread=None,
            yes_bid=0.41,
            yes_ask=0.45,
            no_bid=0.52,
            no_ask=0.58,
        )
    )

    assert row["yes_spread"] == pytest.approx(0.04)
    assert row["no_spread"] == pytest.approx(0.06)
    assert row["min_spread"] == pytest.approx(0.04)
    assert row["spread"] == pytest.approx(0.04)


def test_cli_exposes_record_1s_command() -> None:
    parser = build_parser()

    args = parser.parse_args([
        "--config",
        "configs/default.toml",
        "record-1s",
        "--emit-min-interval-seconds",
        "1",
        "--max-events",
        "2",
    ])

    assert args.command == "record-1s"
    assert args.emit_min_interval_seconds == pytest.approx(1.0)
    assert args.max_events == 2
