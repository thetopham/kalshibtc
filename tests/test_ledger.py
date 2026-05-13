from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pandas as pd

from kalshi_btc_15m_bot.config import BotConfig
from kalshi_btc_15m_bot.ledger import PaperLedger
from kalshi_btc_15m_bot.models import KalshiMarket
from kalshi_btc_15m_bot.predictor import BTC15MPredictor


def frame(n: int = 320) -> pd.DataFrame:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    price = 100.0
    for i in range(n):
        open_price = price
        close = open_price + 0.05
        rows.append(
            {
                "date": start + timedelta(minutes=15 * i),
                "open": open_price,
                "high": close + 0.1,
                "low": open_price - 0.1,
                "close": close,
                "volume": 50 + i,
            }
        )
        price = close
    return pd.DataFrame(rows).set_index("date")


def market(result: str | None = None) -> KalshiMarket:
    now = datetime(2026, 1, 4, 0, 0, tzinfo=UTC)
    raw = {"result": result} if result else {}
    return KalshiMarket(
        ticker="KXBTC15M-TEST-45",
        event_ticker="KXBTC15M-TEST",
        title="BTC price up in next 15 mins?",
        status="active",
        yes_bid=0.39,
        yes_ask=0.40,
        no_bid=0.59,
        no_ask=0.60,
        last_price=0.50,
        target_price=100.0,
        open_time=now - timedelta(minutes=5),
        close_time=now + timedelta(minutes=10),
        expected_expiration_time=now + timedelta(minutes=15),
        volume=1000,
        liquidity=100,
        open_interest=100,
        raw=raw,
    )


def test_ledger_records_opens_and_settles_paper_trade(tmp_path) -> None:
    config = replace(BotConfig(), data_dir=tmp_path)
    ledger = PaperLedger(config.ledger_path, config.paper)
    now = datetime(2026, 1, 4, 0, 0, tzinfo=UTC)
    prediction = BTC15MPredictor(config).predict(frame(), market=market(), current_price=120.0, now=now)
    ledger.record_prediction(prediction)
    trade_id = ledger.maybe_open_paper_trade(prediction)
    assert trade_id is not None
    assert ledger.account().open_trades == 1
    assert ledger.settle_market(market("yes")) == 1
    account = ledger.account()
    assert account.open_trades == 0
    assert account.settled_trades == 1
    assert account.realized_pnl > 0
