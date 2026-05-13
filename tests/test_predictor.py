from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from kalshi_btc_15m_bot.config import BotConfig
from kalshi_btc_15m_bot.models import KalshiMarket
from kalshi_btc_15m_bot.predictor import BTC15MPredictor


def trending_frame(n: int = 320) -> pd.DataFrame:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    price = 100.0
    for i in range(n):
        open_price = price
        drift = 0.08 + (0.03 if i % 5 else -0.01)
        close = open_price + drift
        rows.append(
            {
                "date": start + timedelta(minutes=15 * i),
                "open": open_price,
                "high": close + 0.15,
                "low": open_price - 0.15,
                "close": close,
                "volume": 100 + (i % 20),
            }
        )
        price = close
    return pd.DataFrame(rows).set_index("date")


def test_predictor_emits_buy_yes_when_price_well_above_target() -> None:
    df = trending_frame()
    now = datetime(2026, 1, 4, 0, 0, tzinfo=UTC)
    market = KalshiMarket(
        ticker="KXBTC15M-TEST-45",
        event_ticker="KXBTC15M-TEST",
        title="BTC price up in next 15 mins?",
        status="active",
        yes_bid=0.39,
        yes_ask=0.40,
        no_bid=0.59,
        no_ask=0.60,
        last_price=0.50,
        target_price=float(df.iloc[-1]["close"] * 0.99),
        open_time=now - timedelta(minutes=5),
        close_time=now + timedelta(minutes=10),
        expected_expiration_time=now + timedelta(minutes=15),
        volume=1000,
        liquidity=100,
        open_interest=100,
    )
    prediction = BTC15MPredictor(BotConfig()).predict(
        df, market=market, current_price=float(df.iloc[-1]["close"]), now=now
    )
    assert prediction.action == "BUY_YES"
    assert prediction.side == "YES"
    assert prediction.probability_yes > 0.55
