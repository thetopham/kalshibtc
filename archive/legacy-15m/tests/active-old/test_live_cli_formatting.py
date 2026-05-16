from __future__ import annotations

from kalshi_btc_15m_bot.bot import format_live_auth_check, format_scan, format_status


def test_format_live_auth_check_sanitizes_account_metadata() -> None:
    text = format_live_auth_check(
        {
            "mode": "live",
            "environment": "demo",
            "base_url": "https://external-api.demo.kalshi.co/trade-api/v2",
            "balance_dollars": 123.45,
            "portfolio_value_dollars": 125.67,
            "open_position_count": 2,
            "latest_live_orders": 1,
            "latest_live_fills": 3,
            "boundary": "read-only auth check; no orders submitted",
        }
    )

    assert "Kalshi live auth check" in text
    assert "environment: demo" in text
    assert "balance: $123.45" in text
    assert "open_positions: 2" in text
    assert "read-only auth check; no orders submitted" in text
    assert "KALSHI_API_KEY" not in text


def test_status_format_includes_live_adapter_boundary_when_enabled() -> None:
    text = format_status(
        {
            "ledger_path": "/tmp/paper-ledger.sqlite3",
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
                "win_rate": 0.0,
                "expectancy_dollars": 0.0,
            },
            "latest_predictions": [],
            "latest_trades": [],
            "open_positions": [],
            "latest_live_orders": [{"id": "live-1", "status": "SUBMITTED", "market_ticker": "KXBTC15M-TEST", "notional_dollars": 1.12}],
            "latest_live_fills": [{"fill_id": "fill-1", "market_ticker": "KXBTC15M-TEST", "count": 2.0, "price": 0.56}],
            "live": {
                "environment": "production",
                "realized_pnl": 0.0,
                "open_positions": [],
                "synced_fills": 0,
                "account": {"balance_dollars": 24.50, "portfolio_value_dollars": 24.50, "nonzero_positions": 0},
            },
            "safety": {
                "trading_mode": "live",
                "enable_live_orders": True,
                "live_orders_supported": True,
                "boundary": "guarded live IOC limit orders enabled",
            },
        }
    )

    assert "live mode: guarded live IOC limit orders enabled" in text
    assert "live_balance: $24.50" in text
    assert "paper_equity: $1000.00" in text
    assert "\nequity: $1000.00" not in text
    assert "latest live orders:" in text
    assert "LIVE ORDER live-1" in text
    assert "latest live fills:" in text
    assert "LIVE FILL fill-1" in text


def test_live_scan_format_uses_authenticated_live_balance_not_paper_cash() -> None:
    text = format_scan(
        {
            "market_ticker": "KXBTC15M-TEST-45",
            "market_close_time": "2026-01-04T00:15:00+00:00",
            "target_price": 100_000.0,
            "current_price": 100_050.0,
            "probability_yes": 0.62,
            "probability_no": 0.38,
            "action": "BUY_YES",
            "side": "YES",
            "edge": 0.07,
            "stake_dollars": 5.0,
            "live_order": {"submitted": False, "reason": "unit_test"},
            "paper_account": {"cash": 1023.03, "open_notional": 0.0, "realized_pnl": 23.03},
            "live_account": {"balance_dollars": 24.50, "portfolio_value_dollars": 24.50, "nonzero_positions": 0},
            "live_status": {"open_positions": [], "realized_pnl": 0.0, "synced_fills": 0},
            "safety": {
                "trading_mode": "live",
                "enable_live_orders": True,
                "live_orders_supported": True,
                "boundary": "guarded live IOC limit orders enabled",
            },
            "reasons": ["unit-test"],
        }
    )

    assert "live_balance: $24.50" in text
    assert "paper_ledger: isolated; not used for live sizing" in text
    assert "$1023.03" not in text
    assert "cash: $1023.03" not in text
