from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kalshi_btc_15m_bot.dashboard import (
    _is_authorized,
    _validate_dashboard_auth,
    collect_dashboard_data,
    render_dashboard_html,
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
