from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from kalshi_btc_15m_bot.dashboard import (
    DEFAULT_SERVICE_NAMES,
    StreamSnapshotStore,
    _is_authorized,
    _sanitize_dashboard_log_message,
    _stream_collector_loop,
    _validate_dashboard_auth,
    collect_dashboard_data,
    collect_stream_dashboard_data,
    dashboard_health_response,
    render_dashboard_html,
    render_stream_dashboard_html,
)


class FakeBot:
    def __init__(self, ledger_path: Path | None = None) -> None:
        ledger_path = ledger_path or Path("/tmp/kbtc15-live/paper-ledger.sqlite3")
        self.config = SimpleNamespace(
            trading_mode="live",
            enable_live_orders=True,
            data_dir=ledger_path.parent,
            ledger_path=ledger_path,
            paper_results_1s_path=ledger_path,
            realtime_snapshots_path=ledger_path.parent / "realtime-snapshots-1s.sqlite3",
            kalshi=SimpleNamespace(series_ticker="KXBTC15M"),
            live=SimpleNamespace(environment="production"),
            market_data=SimpleNamespace(provider="coinbase", granularity_seconds=900),
        )

    def _safety_payload(self) -> dict[str, object]:
        return {
            "trading_mode": "live",
            "enable_live_orders": True,
            "live_orders_supported": True,
            "boundary": "guarded live IOC limit orders enabled",
        }

    def status(self) -> dict[str, object]:
        return {
            "ledger_path": str(self.config.ledger_path),
            "paper_account": {
                "cash": 1000.0,
                "open_notional": 0.0,
                "realized_pnl": 0.0,
                "open_trades": 0,
                "settled_trades": 0,
            },
            "performance": {
                "total_equity": 1000.0,
                "open_unrealized_pnl": 0.0,
                "win_rate": 0.5,
                "expectancy_dollars": 1.25,
                "total_trades": 4,
                "closed_trades": 4,
                "open_trades": 0,
            },
            "latest_predictions": [
                {
                    "created_at": "2026-05-13T00:00:00+00:00",
                    "market_ticker": "KXBTC15M-TEST",
                    "action": "BUY_YES",
                    "probability_yes": 0.61,
                    "edge": 0.04,
                }
            ],
            "latest_trades": [],
            "open_positions": [],
            "live": {
                "environment": "production",
                "base_url": "https://external-api.kalshi.com/trade-api/v2",
                "account": {
                    "balance_dollars": 24.50,
                    "portfolio_value_dollars": 24.50,
                    "nonzero_positions": 0,
                },
                "open_positions": [],
                "latest_orders": [],
                "latest_fills": [],
                "realized_pnl": 0.0,
                "synced_fills": 0,
            },
            "safety": self._safety_payload(),
        }


def test_collect_dashboard_data_projects_1s_paper_only_fields() -> None:
    data = collect_dashboard_data(
        FakeBot(),
        include_service_status=False,
        scan_interval_seconds=60,
    )

    assert data["boundary"] == "Read-only dashboard. No live orders. Active status page shows 1s paper trading data only."
    assert data["strategy"]["trading_mode"] == "live"
    assert data["strategy"]["scan_interval_seconds"] == 60
    assert data["strategy"]["candle_granularity_seconds"] == 900
    assert "live_balance_dollars" not in data["portfolio"]
    assert "live_portfolio_value_dollars" not in data["portfolio"]
    assert "live_realized_pnl" not in data["portfolio"]
    assert "paper_total_pnl" in data["portfolio"]
    assert data["latest_predictions"] == []
    assert isinstance(data["latest_paper_trades"], list)
    assert "open_live_positions" not in data
    assert "latest_live_orders" not in data
    assert "latest_live_fills" not in data


def test_default_dashboard_services_include_only_active_1s_paper_stack() -> None:
    assert DEFAULT_SERVICE_NAMES == (
        "kalshi-btc15m-1s-recorder.service",
        "kalshi-btc15m-1s-paper.service",
        "kalshi-btc15m-dashboard.service",
    )
    assert "kalshi-btc15m-live-prod.service" not in DEFAULT_SERVICE_NAMES
    assert "kalshi-btc15m-live-demo.service" not in DEFAULT_SERVICE_NAMES
    assert "kalshi-btc15m-paper.service" not in DEFAULT_SERVICE_NAMES


def _write_paper_performance_db(ledger_path: Path) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ledger_path) as conn:
        conn.executescript(
            """
            CREATE TABLE predictions (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                event_ticker TEXT NOT NULL,
                market_close_time TEXT,
                action TEXT NOT NULL,
                side TEXT,
                strategy TEXT,
                probability_yes REAL NOT NULL,
                probability_no REAL NOT NULL,
                confidence REAL NOT NULL,
                edge REAL NOT NULL,
                stake_dollars REAL NOT NULL,
                current_price REAL NOT NULL,
                target_price REAL,
                yes_ask REAL NOT NULL,
                no_ask REAL NOT NULL,
                model_info_json TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                features_json TEXT NOT NULL,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE paper_trades (
                id TEXT PRIMARY KEY,
                prediction_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                contracts REAL NOT NULL,
                notional REAL NOT NULL,
                status TEXT NOT NULL,
                market_close_time TEXT,
                settlement_result TEXT,
                realized_pnl REAL,
                settled_at TEXT,
                exit_price REAL,
                exit_reason TEXT,
                settlement_source TEXT,
                official_result TEXT,
                official_expiration_value REAL,
                settlement_value_dollars REAL
            );
            """
        )
        for prediction_id, action, side, market_ticker in [
            ("p-win", "BUY_YES", "YES", "KXBTC15M-TEST-WIN"),
            ("p-loss", "BUY_NO", "NO", "KXBTC15M-TEST-LOSS"),
            ("p-open", "BUY_YES", "YES", "KXBTC15M-TEST-OPEN"),
        ]:
            conn.execute(
                """
                INSERT INTO predictions (
                    id, created_at, market_ticker, event_ticker, market_close_time,
                    action, side, strategy, probability_yes, probability_no, confidence, edge,
                    stake_dollars, current_price, target_price, yes_ask, no_ask,
                    model_info_json, reasons_json, features_json, raw_json
                ) VALUES (?, '2026-05-14T16:00:00+00:00', ?, 'KXBTC15M-TEST',
                    '2026-05-14T16:15:00+00:00', ?, ?, 'simple_directional', 0.60, 0.40, 0.30, 0.05,
                    25.0, 100000.0, 100010.0, 0.55, 0.45, '{}',
                    '["above strike + trend up"]',
                    '{"btc_velocity_30s": 2.5, "distance_from_strike": -10.0, "seconds_to_expiry": 899.0}',
                    '{}')
                """,
                (prediction_id, market_ticker, action, side),
            )
        conn.executemany(
            """
            INSERT INTO paper_trades (
                id, prediction_id, created_at, market_ticker, side, entry_price,
                contracts, notional, status, market_close_time, realized_pnl,
                settled_at, exit_price, exit_reason, settlement_source,
                official_result, official_expiration_value, settlement_value_dollars
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "paper-win",
                    "p-win",
                    "2026-05-14T16:00:01+00:00",
                    "KXBTC15M-TEST-WIN",
                    "YES",
                    0.50,
                    40.0,
                    20.0,
                    "SETTLED",
                    "2026-05-14T16:15:00+00:00",
                    10.0,
                    "2026-05-14T16:05:00+00:00",
                    0.75,
                    "1s_expiry_above",
                    "coinbase_estimate",
                    None,
                    None,
                    None,
                ),
                (
                    "paper-loss",
                    "p-loss",
                    "2026-05-14T16:10:01+00:00",
                    "KXBTC15M-TEST-LOSS",
                    "NO",
                    0.40,
                    30.0,
                    12.0,
                    "SETTLED",
                    "2026-05-14T16:15:00+00:00",
                    -4.0,
                    "2026-05-14T16:15:30+00:00",
                    0.0,
                    "settlement_yes",
                    "kalshi_official",
                    "yes",
                    100001.25,
                    1.0,
                ),
                (
                    "paper-open",
                    "p-open",
                    "2026-05-14T16:12:01+00:00",
                    "KXBTC15M-TEST-OPEN",
                    "YES",
                    0.50,
                    40.0,
                    20.0,
                    "OPEN",
                    "2026-05-14T16:15:00+00:00",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
            ],
        )


def _write_realtime_snapshot_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE realtime_snapshots_1s (
                ts TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                yes_bid REAL,
                no_bid REAL,
                yes_ask REAL,
                no_ask REAL,
                PRIMARY KEY (market_ticker, ts)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO realtime_snapshots_1s (ts, market_ticker, yes_bid, no_bid, yes_ask, no_ask)
            VALUES ('2026-05-14T16:12:30+00:00', 'KXBTC15M-TEST-OPEN', 0.60, 0.39, 0.62, 0.41)
            """
        )


def test_collect_dashboard_data_adds_sqlite_paper_performance_summary(tmp_path: Path) -> None:
    ledger_path = tmp_path / "paper-ledger.sqlite3"
    _write_paper_performance_db(ledger_path)
    _write_realtime_snapshot_db(tmp_path / "realtime-snapshots-1s.sqlite3")

    data = collect_dashboard_data(FakeBot(ledger_path), include_service_status=False)
    perf = data["paper_performance"]
    metrics = perf["metrics"]

    assert metrics["total_trades"] == 3
    assert metrics["total_simulated_positions"] == 3
    assert metrics["open_positions"] == 1
    assert metrics["closed_positions"] == 2
    assert metrics["realized_pnl"] == pytest.approx(6.0)
    assert metrics["unrealized_pnl"] == pytest.approx(4.0)
    assert metrics["total_pnl"] == pytest.approx(10.0)
    assert metrics["win_rate"] == pytest.approx(0.5)
    assert metrics["avg_win"] == pytest.approx(10.0)
    assert metrics["avg_loss"] == pytest.approx(-4.0)
    assert metrics["largest_win"] == pytest.approx(10.0)
    assert metrics["largest_loss"] == pytest.approx(-4.0)
    assert metrics["marked_open_positions"] == 1
    assert perf["cumulative_pnl"][-1]["cumulative_pnl"] == pytest.approx(6.0)
    assert perf["open_positions"][0]["unrealized_pnl"] == pytest.approx(4.0)
    assert any(row["signal"] == "BUY_YES" for row in perf["by_signal"])
    assert any(row["market_ticker"] == "KXBTC15M-TEST-WIN" for row in perf["by_market"])

    review = perf["review_trades"]
    assert review[0]["strategy"] == "simple_directional"
    assert review[0]["side"] == "YES"
    assert review[0]["entry_time"] == "2026-05-14T16:12:01+00:00"
    assert review[0]["exit_time"] is None
    assert review[0]["entry_price"] == pytest.approx(0.50)
    assert review[0]["exit_price"] is None
    assert review[0]["pnl"] == pytest.approx(4.0)
    assert review[0]["hold_seconds"] == pytest.approx(29.0)
    assert review[0]["slope_at_entry"] == pytest.approx(2.5)
    assert review[0]["distance_from_strike"] == pytest.approx(-10.0)
    assert review[0]["seconds_to_expiry"] == pytest.approx(899.0)
    assert review[0]["reason"] == "above strike + trend up"
    assert review[1]["settlement_source"] == "kalshi_official"
    assert review[1]["settlement_source_label"] == "official (Kalshi)"
    assert review[1]["official_result"] == "yes"
    assert review[1]["official_expiration_value"] == pytest.approx(100001.25)
    assert review[2]["settlement_source"] == "coinbase_estimate"
    assert review[2]["settlement_source_label"] == "estimated (Coinbase/raw)"

    recent = perf["recent_trades"]
    assert recent[1]["settlement_source"] == "kalshi_official"
    assert recent[1]["settlement_source_label"] == "official (Kalshi)"
    assert recent[2]["settlement_source"] == "coinbase_estimate"
    assert recent[2]["settlement_source_label"] == "estimated (Coinbase/raw)"

    buckets = perf["review_buckets"]
    by_strategy = {row["bucket"]: row for row in buckets["strategy"]}
    assert by_strategy["simple_directional"]["trades"] == 3
    assert by_strategy["simple_directional"]["win_rate"] == pytest.approx(2 / 3)
    assert by_strategy["simple_directional"]["avg_pnl"] == pytest.approx(10.0 / 3)
    assert by_strategy["simple_directional"]["total_pnl"] == pytest.approx(10.0)
    assert by_strategy["simple_directional"]["avg_hold_seconds"] == pytest.approx(219.0)

    by_side = {row["bucket"]: row for row in buckets["side"]}
    assert by_side["YES"]["trades"] == 2
    assert by_side["YES"]["total_pnl"] == pytest.approx(14.0)
    assert by_side["NO"]["trades"] == 1
    assert by_side["NO"]["total_pnl"] == pytest.approx(-4.0)

    assert {row["bucket"] for row in buckets["seconds_to_expiry"]} == {"420-900"}
    assert buckets["seconds_to_expiry"][0]["trades"] == 3
    assert buckets["distance_from_strike"][0]["bucket"] == "close:10-25"
    assert buckets["distance_from_strike"][0]["trades"] == 3
    assert buckets["slope_at_entry"][0]["bucket"] == "medium:1-3"
    assert buckets["slope_at_entry"][0]["trades"] == 3
    by_hold = {row["bucket"]: row for row in buckets["hold_seconds"]}
    assert by_hold["0-60"]["trades"] == 1
    assert by_hold["180-420"]["trades"] == 2


def test_render_dashboard_html_includes_paper_trading_performance_panel(tmp_path: Path) -> None:
    ledger_path = tmp_path / "paper-ledger.sqlite3"
    _write_paper_performance_db(ledger_path)
    _write_realtime_snapshot_db(tmp_path / "realtime-snapshots-1s.sqlite3")

    text = render_dashboard_html(collect_dashboard_data(FakeBot(ledger_path), include_service_status=False))

    assert "Paper Trading Performance" in text
    assert "cumulative-pnl-chart" in text
    assert "Recent paper trades" in text
    assert "Paper PnL Review" in text
    assert "Paper Review Buckets" in text
    assert "seconds_to_expiry" in text
    assert "distance_from_strike" in text
    assert "slope_at_entry" in text
    assert "hold_seconds" in text
    assert "medium:1-3" in text
    assert "simple_directional" in text
    assert "above strike + trend up" in text
    assert "Grouped by signal" in text
    assert "+$6.00" in text
    assert "+$4.00" in text
    assert "official (Kalshi)" in text
    assert "estimated (Coinbase/raw)" in text
    assert "settlement: official (Kalshi)" in text
    assert "settlement: estimated (Coinbase/raw)" in text


def test_render_dashboard_html_escapes_paper_review_text() -> None:
    data = collect_dashboard_data(FakeBot(), include_service_status=False)
    data["paper_performance"]["review_trades"] = [
        {
            "strategy": "<b>simple</b>",
            "side": "YES",
            "entry_time": "2026-05-14T16:00:00+00:00",
            "exit_time": None,
            "entry_price": 0.5,
            "exit_price": None,
            "pnl": 1.25,
            "hold_seconds": 12.0,
            "slope_at_entry": 2.0,
            "distance_from_strike": 10.0,
            "seconds_to_expiry": 800.0,
            "reason": "<script>alert(1)</script>",
        }
    ]

    data["paper_performance"]["review_buckets"] = {
        "strategy": [
            {
                "bucket": "<img src=x onerror=alert(1)>",
                "trades": 1,
                "win_rate": 1.0,
                "avg_pnl": 1.25,
                "total_pnl": 1.25,
                "avg_hold_seconds": 12.0,
            }
        ]
    }

    text = render_dashboard_html(data)

    assert "<b>simple</b>" not in text
    assert "<script>alert(1)</script>" not in text
    assert "<img src=x onerror=alert(1)>" not in text
    assert "&lt;b&gt;simple&lt;/b&gt;" in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "&lt;img src=x onerror=alert(1)&gt;" in text


def test_render_dashboard_html_includes_cards_and_read_only_boundary() -> None:
    data = collect_dashboard_data(
        FakeBot(),
        include_service_status=False,
        scan_interval_seconds=60,
    )
    text = render_dashboard_html(data)

    assert "Kalshi BTC 15m Dashboard" in text
    assert "1s Paper Trading Status" in text
    assert "Live balance" not in text
    assert "Live portfolio" not in text
    assert "Live realized PnL" not in text
    assert "Open live positions" not in text
    assert "Latest live orders/fills" not in text
    assert "live-prod" not in text
    assert "live-demo" not in text
    assert "paper-ledger.sqlite3" not in text
    assert "kalshi-btc15m-paper.service" not in text
    assert "orders enabled with caps" not in text
    assert "$24.50" not in text
    assert "scan: 60s" in text
    assert "candles: 900s" in text
    assert "No live orders" in text
    assert "no route that can scan, submit, cancel, or exit orders" in text
    assert "KALSHI_API_KEY" not in text


def _sample_stream_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "event": "market_state",
        "as_of": "2026-05-14T17:01:02+00:00",
        "btc_source": "coinbase_ws",
        "btc_product": "BTC-USD",
        "btc_ts": "2026-05-14T17:01:01+00:00",
        "current_price": 64321.5,
        "btc_bid": 64321.0,
        "btc_ask": 64322.0,
        "market_ticker": "KXBTC15M-TEST",
        "target_price": 64250.0,
        "seconds_to_close": 328.0,
        "seconds_to_expiration": 328.0,
        "direction_state": "ABOVE_TARGET",
        "probability_yes": 0.602,
        "probability_no": 0.398,
        "model_probability_yes": 0.50,
        "model_probability_gap": 0.102,
        "market_implied_yes": 0.55,
        "yes_bid": 0.54,
        "yes_ask": 0.56,
        "no_bid": 0.43,
        "no_ask": 0.46,
        "orderbook_liquidity": 1200.0,
        "orderbook_valid": True,
        "best_ev_side": "YES",
        "best_ev_per_dollar": 0.075,
        "best_ev_reference_profit_dollars": 1.875,
        "best_edge": 0.042,
        "best_spread": 0.02,
        "monitor_action": "NO_EDGE",
        "monitor_side": "NONE",
        "decision": "WATCH_ONLY_MODEL_DISAGREEMENT",
        "execution_decision": {
            "action": "NO_TRADE",
            "side": "NONE",
            "size_dollars": 0.0,
            "entry_price": None,
            "stop_type": "none",
            "stop_price": None,
            "take_profit_price": None,
            "confidence": 0.20,
            "regime": "flat_chop",
            "reason": "model/market disagreement blocks execution",
            "blocked_by": ["model_state_probability_gap"],
        },
        "warnings": ["model_state_probability_gap"],
        "boundary": "read-only websocket market-state stream; no orders submitted",
    }
    payload.update(overrides)
    return payload


def test_stream_snapshot_store_tracks_latest_payload_and_reconnect_warnings() -> None:
    store = StreamSnapshotStore()

    store.record_line(json.dumps({"warning": "kalshi_ws_reconnect_failed", "error": "boom"}))
    store.record_line(json.dumps(_sample_stream_payload()))
    snapshot = store.snapshot()

    assert snapshot["events_seen"] == 1
    assert snapshot["warning_count"] == 1
    assert snapshot["last_warning"]["warning"] == "kalshi_ws_reconnect_failed"
    assert snapshot["latest"]["market_ticker"] == "KXBTC15M-TEST"
    assert snapshot["latest"]["decision"] == "WATCH_ONLY_MODEL_DISAGREEMENT"


def test_stream_snapshot_store_tracks_chart_history_points() -> None:
    store = StreamSnapshotStore()
    for idx, price in enumerate([64300.0, 64350.0, 64425.0]):
        store.record_line(
            json.dumps(
                _sample_stream_payload(
                    as_of=f"2026-05-14T17:01:0{idx}+00:00",
                    current_price=price,
                    target_price=64250.0,
                    probability_yes=0.55 + idx * 0.02,
                    probability_no=0.45 - idx * 0.02,
                    yes_bid=0.51 + idx * 0.01,
                    yes_ask=0.53 + idx * 0.01,
                    no_bid=0.45 - idx * 0.01,
                    no_ask=0.47 - idx * 0.01,
                    best_ev_per_dollar=0.02 + idx * 0.015,
                    best_edge=0.01 + idx * 0.005,
                    seconds_to_close=328.0 - idx,
                )
            )
        )

    history = store.snapshot()["history"]

    assert len(history) == 3
    assert history[-1]["as_of"] == "2026-05-14T17:01:02+00:00"
    assert history[-1]["current_price"] == 64425.0
    assert history[-1]["target_price"] == 64250.0
    assert history[-1]["probability_yes"] == pytest.approx(0.59)
    assert history[-1]["best_ev_per_dollar"] == pytest.approx(0.05)
    assert history[-1]["seconds_to_close"] == 326.0


def test_collect_stream_dashboard_data_projects_latest_stream_state() -> None:
    store = StreamSnapshotStore()
    store.record_line(json.dumps(_sample_stream_payload()))

    data = collect_stream_dashboard_data(
        FakeBot(),
        store,
        include_service_status=False,
        stream_emit_min_interval_seconds=1.0,
    )

    assert data["boundary"] == "guarded live IOC limit orders enabled"
    assert data["strategy"]["stream_emit_min_interval_seconds"] == 1.0
    assert data["stream"]["latest"]["market_ticker"] == "KXBTC15M-TEST"
    assert data["stream"]["latest"]["monitor_action"] == "NO_EDGE"
    assert data["stream"]["history"][-1]["current_price"] == 64321.5


def test_collect_stream_dashboard_data_projects_supabase_and_stream_paper_fields() -> None:
    store = StreamSnapshotStore()
    store.record_line(
        json.dumps(
            _sample_stream_payload(
                feature_source="supabase_tv_datafeed",
                feature_stale=False,
                supabase_features={
                    "source": "supabase_tv_datafeed",
                    "symbol": "BTCUSD",
                    "timeframe": 1,
                    "feature_age_seconds": 12.0,
                    "stale": False,
                    "atr": 120.0,
                },
                stream_paper={
                    "enabled": True,
                    "opened_side": "YES",
                    "managed_positions": [
                        {
                            "trade_id": "paper-test",
                            "mark_price": 0.73,
                            "exit_signal": "hold",
                            "paper_closed": False,
                        }
                    ],
                },
            )
        )
    )

    data = collect_stream_dashboard_data(FakeBot(), store, include_service_status=False)

    latest = data["stream"]["latest"]
    assert latest["feature_source"] == "supabase_tv_datafeed"
    assert latest["feature_stale"] is False
    assert latest["supabase_features"]["atr"] == pytest.approx(120.0)
    assert latest["stream_paper"]["managed_positions"][0]["trade_id"] == "paper-test"
    assert data["stream"]["history"][-1]["feature_stale"] is False
    assert data["stream"]["history"][-1]["feature_age_seconds"] == pytest.approx(12.0)


def test_render_stream_dashboard_html_includes_live_stream_shell_and_latest_state() -> None:
    store = StreamSnapshotStore()
    store.record_line(json.dumps(_sample_stream_payload()))
    data = collect_stream_dashboard_data(
        FakeBot(),
        store,
        include_service_status=False,
        stream_emit_min_interval_seconds=1.0,
    )

    text = render_stream_dashboard_html(data)

    assert "Kalshi BTC Stream" in text
    assert "/api/stream" in text
    assert "KXBTC15M-TEST" in text
    assert "Execution Decision v1" in text
    assert "ACTION" in text
    assert "NO_TRADE" in text
    assert "id=\"execution-action\"" in text
    assert "id=\"execution-size\"" in text
    assert "id=\"execution-entry\"" in text
    assert "id=\"execution-stop\"" in text
    assert "id=\"execution-take-profit\"" in text
    assert "id=\"execution-confidence\"" in text
    assert "id=\"execution-regime\"" in text
    assert "id=\"execution-reason\"" in text
    assert "id=\"execution-blocked-by\"" in text
    assert "id=\"stream-health\"" in text
    assert "model/market disagreement blocks execution" in text
    assert "Debug internals" in text
    assert "Prediction reasons" in text
    assert "old probability model" in text
    assert "best_ev" in text
    assert "prob_edge" in text
    assert "Live graph" in text
    assert "stream-chart" in text
    assert "chart-metric" in text
    assert "renderChart" in text
    assert "stream.history" in text
    assert "no live orders submitted" in text
    assert "KALSHI_API_KEY" not in text


def test_stream_collector_loop_restarts_after_transient_failure() -> None:
    store = StreamSnapshotStore()
    attempts: list[float] = []
    sleeps: list[float] = []

    def run_once(_bot: FakeBot, stream_store: StreamSnapshotStore, *, emit_min_interval_seconds: float) -> int:
        attempts.append(emit_min_interval_seconds)
        if len(attempts) == 1:
            raise RuntimeError("no close frame received or sent")
        stream_store.record_line(json.dumps(_sample_stream_payload(as_of="2026-05-14T17:01:03+00:00")))
        return 0

    _stream_collector_loop(
        FakeBot(),
        store,
        emit_min_interval_seconds=0.5,
        run_once=run_once,
        sleep=sleeps.append,
        max_attempts=2,
    )

    snapshot = store.snapshot()
    assert attempts == [0.5, 0.5]
    assert sleeps == [pytest.approx(1.0)]
    assert snapshot["events_seen"] == 1
    assert snapshot["latest"]["as_of"] == "2026-05-14T17:01:03+00:00"
    assert snapshot["running"] is False
    assert snapshot["warnings"][0]["warning"] == "stream_collector_error"


def test_dashboard_log_sanitizer_removes_query_token() -> None:
    message = '"GET /api/stream?token=secret-token HTTP/1.1" 200 -'

    sanitized = _sanitize_dashboard_log_message("/api/stream?token=secret-token", message)

    assert "secret-token" not in sanitized
    assert sanitized == '"GET /api/stream HTTP/1.1" 200 -'


def test_dashboard_auth_requires_token_for_public_bind() -> None:
    with pytest.raises(ValueError, match="requires KALSHI_BTC15M_DASHBOARD_TOKEN"):
        _validate_dashboard_auth("0.0.0.0", None)
    _validate_dashboard_auth("127.0.0.1", None)
    _validate_dashboard_auth("0.0.0.0", "secret-token")


def test_dashboard_health_response_is_ok_without_stream() -> None:
    payload, status = dashboard_health_response(stream_enabled=False, stream_snapshot=None)

    assert status.value == 200
    assert payload["ok"] is True
    assert payload["reasons"] == []


def test_dashboard_health_response_returns_503_for_stream_failures() -> None:
    payload, status = dashboard_health_response(
        stream_enabled=True,
        stream_snapshot={
            "running": False,
            "events_seen": 2,
            "updated_at": "2026-05-14T17:01:02+00:00",
            "staleness_seconds": 99.0,
            "latest": {"market_ticker": "KXBTC15M-TEST"},
            "last_error": "kalshi_ws_auth_reconnect_failed",
        },
        max_staleness_seconds=10.0,
    )

    assert status.value == 503
    assert payload["ok"] is False
    assert "stream_not_running" in payload["reasons"]
    assert "stream_latest_stale" in payload["reasons"]
    assert "stream_last_error" in payload["reasons"]
    assert payload["stream"]["staleness_seconds"] == 99.0


def test_dashboard_auth_accepts_bearer_or_query_token() -> None:
    assert _is_authorized(None, "", None)
    assert _is_authorized("secret", "", "Bearer secret")
    assert _is_authorized("secret", "token=secret", None)
    assert not _is_authorized("secret", "token=wrong", None)
    assert not _is_authorized("secret", "", "Bearer wrong")
