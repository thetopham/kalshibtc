from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kalshi_btc_15m_bot.dashboard import (
    StreamSnapshotStore,
    _is_authorized,
    _validate_dashboard_auth,
    collect_dashboard_data,
    collect_stream_dashboard_data,
    render_dashboard_html,
    render_stream_dashboard_html,
)


class FakeBot:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            trading_mode="live",
            enable_live_orders=True,
            data_dir=Path("/tmp/kbtc15-live"),
            ledger_path=Path("/tmp/kbtc15-live/paper-ledger.sqlite3"),
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


def test_collect_dashboard_data_projects_live_and_strategy_fields() -> None:
    data = collect_dashboard_data(
        FakeBot(),
        include_service_status=False,
        scan_interval_seconds=60,
    )

    assert data["boundary"] == "guarded live IOC limit orders enabled"
    assert data["strategy"]["trading_mode"] == "live"
    assert data["strategy"]["scan_interval_seconds"] == 60
    assert data["strategy"]["candle_granularity_seconds"] == 900
    assert data["portfolio"]["live_balance_dollars"] == 24.50
    assert data["portfolio"]["paper_equity"] == 1000.0
    assert data["latest_predictions"][0]["action"] == "BUY_YES"


def test_render_dashboard_html_includes_cards_and_read_only_boundary() -> None:
    data = collect_dashboard_data(
        FakeBot(),
        include_service_status=False,
        scan_interval_seconds=60,
    )
    text = render_dashboard_html(data)

    assert "Kalshi BTC 15m Dashboard" in text
    assert "Live balance" in text
    assert "$24.50" in text
    assert "scan: 60s" in text
    assert "candles: 900s" in text
    assert "guarded live IOC limit orders enabled" in text
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
    assert "WATCH_ONLY_MODEL_DISAGREEMENT" in text
    assert "NO_EDGE" in text
    assert "best_ev" in text
    assert "prob_edge" in text
    assert "Live graph" in text
    assert "stream-chart" in text
    assert "chart-metric" in text
    assert "renderChart" in text
    assert "stream.history" in text
    assert "no live orders submitted" in text
    assert "KALSHI_API_KEY" not in text


def test_dashboard_auth_requires_token_for_public_bind() -> None:
    with pytest.raises(ValueError, match="requires KALSHI_BTC15M_DASHBOARD_TOKEN"):
        _validate_dashboard_auth("0.0.0.0", None)
    _validate_dashboard_auth("127.0.0.1", None)
    _validate_dashboard_auth("0.0.0.0", "secret-token")


def test_dashboard_auth_accepts_bearer_or_query_token() -> None:
    assert _is_authorized(None, "", None)
    assert _is_authorized("secret", "", "Bearer secret")
    assert _is_authorized("secret", "token=secret", None)
    assert not _is_authorized("secret", "token=wrong", None)
    assert not _is_authorized("secret", "", "Bearer wrong")
