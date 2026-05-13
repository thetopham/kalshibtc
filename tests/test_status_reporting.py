from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kalshi_btc_15m_bot.bot import format_status
from kalshi_btc_15m_bot.ledger import mark_open_trade_to_market
from kalshi_btc_15m_bot.models import KalshiMarket


def market(*, yes_bid: float = 0.55, yes_ask: float = 0.57, no_bid: float = 0.43, no_ask: float = 0.45) -> KalshiMarket:
    now = datetime(2026, 1, 4, 0, 0, tzinfo=UTC)
    return KalshiMarket(
        ticker="KXBTC15M-TEST-45",
        event_ticker="KXBTC15M-TEST",
        title="BTC price up in next 15 mins?",
        status="active",
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        last_price=0.56,
        target_price=100_000.0,
        open_time=now - timedelta(minutes=5),
        close_time=now + timedelta(minutes=10),
        expected_expiration_time=now + timedelta(minutes=15),
        volume=1_000,
        liquidity=250.0,
        open_interest=100,
        raw={},
    )


def open_trade() -> dict[str, object]:
    return {
        "id": "paper-test",
        "prediction_id": "prediction-test",
        "created_at": "2026-01-04T00:00:00+00:00",
        "market_ticker": "KXBTC15M-TEST-45",
        "side": "YES",
        "entry_price": 0.40,
        "contracts": 62.5,
        "notional": 25.0,
        "status": "OPEN",
        "market_close_time": "2026-01-04T00:10:00+00:00",
        "settlement_result": None,
        "realized_pnl": None,
        "settled_at": None,
    }


def test_mark_open_trade_to_market_uses_exit_bid_for_unrealized_pnl() -> None:
    mark = mark_open_trade_to_market(open_trade(), market(yes_bid=0.55))

    assert mark["trade_id"] == "paper-test"
    assert mark["price_source"] == "yes_bid"
    assert mark["mark_price"] == 0.55
    assert mark["current_value"] == pytest.approx(34.375)
    assert mark["unrealized_pnl"] == pytest.approx(9.375)
    assert mark["unrealized_pnl_pct"] == pytest.approx(0.375)
    assert mark["max_profit_if_correct"] == pytest.approx(37.5)


def test_status_format_calls_out_open_paper_position_mark_to_market() -> None:
    mark = mark_open_trade_to_market(open_trade(), market(yes_bid=0.55))
    text = format_status(
        {
            "ledger_path": "/tmp/paper-ledger.sqlite3",
            "paper_account": {
                "cash": 975.0,
                "open_notional": 25.0,
                "realized_pnl": 0.0,
                "open_trades": 1,
                "settled_trades": 0,
            },
            "latest_predictions": [],
            "latest_trades": [open_trade()],
            "open_positions": [mark],
            "safety": {"live_orders_supported": False},
        }
    )

    assert "open paper positions:" in text
    assert "PAPER OPEN paper-test" in text
    assert "entry=0.400" in text
    assert "mark=0.550" in text
    assert "unrealized=+$9.38 (+37.5%)" in text
    assert "max_win=+$37.50" in text
    assert "liquidity=$250.00" in text
