from __future__ import annotations

from kalshi_btc_15m_bot.bot import format_report


def test_format_report_displays_operator_performance_and_risk_boundary() -> None:
    text = format_report(
        {
            "ledger_path": "/tmp/paper.sqlite3",
            "performance": {
                "total_trades": 3,
                "closed_trades": 2,
                "open_trades": 1,
                "winning_trades": 1,
                "losing_trades": 1,
                "win_rate": 0.5,
                "realized_pnl": -15.625,
                "open_unrealized_pnl": 9.375,
                "total_equity": 993.75,
                "expectancy_dollars": -7.8125,
                "realized_roi_on_risk": -0.3125,
                "largest_win": 9.375,
                "largest_loss": -25.0,
            },
            "open_positions": [
                {
                    "trade_id": "paper-test",
                    "market_ticker": "KXBTC15M-TEST-45",
                    "side": "YES",
                    "entry_price": 0.40,
                    "mark_price": 0.55,
                    "unrealized_pnl": 9.375,
                    "unrealized_pnl_pct": 0.375,
                    "exit_signal": "take_profit",
                    "market_close_time": "2026-01-04T00:10:00+00:00",
                }
            ],
            "safety": {
                "trading_mode": "paper",
                "enable_live_orders": False,
                "live_orders_supported": False,
            },
        }
    )

    assert "BTC 15m Kalshi operator report" in text
    assert "total_equity: $993.75" in text
    assert "realized_pnl: -$15.62" in text
    assert "open_unrealized: +$9.38" in text
    assert "win_rate: 50.0%" in text
    assert "expectancy: -$7.81/trade" in text
    assert "PAPER OPEN paper-test" in text
    assert "exit_signal=take_profit" in text
    assert "boundary: paper-only; live_orders_supported=false" in text
