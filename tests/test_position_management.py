from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kalshi_btc_15m_bot.config import PaperConfig
from kalshi_btc_15m_bot.ledger import PaperLedger, evaluate_paper_exit, mark_open_trade_to_market
from kalshi_btc_15m_bot.models import KalshiMarket, ModelInfo, Prediction


def market(
    *,
    yes_bid: float = 0.55,
    yes_ask: float = 0.57,
    no_bid: float = 0.43,
    no_ask: float = 0.45,
    close_time: datetime | None = None,
) -> KalshiMarket:
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
        close_time=close_time or now + timedelta(minutes=10),
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
        "exit_price": None,
        "exit_reason": None,
    }


def prediction_for(test_market: KalshiMarket) -> Prediction:
    return Prediction(
        prediction_id="prediction-skip-test",
        created_at=datetime(2026, 1, 4, 0, 0, tzinfo=UTC),
        market=test_market,
        current_price=100_100.0,
        probability_yes=0.60,
        probability_no=0.40,
        action="BUY_YES",
        side="YES",
        edge=0.15,
        confidence=0.20,
        stake_dollars=25.0,
        reasons=[],
        model_info=ModelInfo(
            name="test-model",
            trained=False,
            samples=0,
            test_accuracy=None,
            brier=None,
            weight=0.0,
        ),
        feature_snapshot={},
    )


def insert_open_trade(ledger: PaperLedger) -> None:
    with ledger.connect() as conn:
        conn.execute(
            """
            INSERT INTO predictions (
                id, created_at, market_ticker, event_ticker, market_close_time,
                action, side, probability_yes, probability_no, confidence, edge,
                stake_dollars, current_price, target_price, yes_ask, no_ask,
                model_info_json, reasons_json, features_json, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "prediction-test",
                "2026-01-04T00:00:00+00:00",
                "KXBTC15M-TEST-45",
                "KXBTC15M-TEST",
                "2026-01-04T00:10:00+00:00",
                "BUY_YES",
                "YES",
                0.60,
                0.40,
                0.20,
                0.20,
                25.0,
                100_100.0,
                100_000.0,
                0.40,
                0.60,
                "{}",
                "[]",
                "{}",
                "{}",
            ),
        )
        conn.execute(
            """
            INSERT INTO paper_trades (
                id, prediction_id, created_at, market_ticker, side, entry_price,
                contracts, notional, status, market_close_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
            """,
            (
                "paper-test",
                "prediction-test",
                "2026-01-04T00:00:00+00:00",
                "KXBTC15M-TEST-45",
                "YES",
                0.40,
                62.5,
                25.0,
                "2026-01-04T00:10:00+00:00",
            ),
        )


def test_paper_entry_skip_reason_explains_wide_spread(tmp_path) -> None:
    ledger = PaperLedger(tmp_path / "paper.sqlite3", PaperConfig(max_spread=0.05))
    wide_spread_market = market(yes_bid=0.20, yes_ask=0.40)

    reason = ledger.paper_entry_skip_reason(prediction_for(wide_spread_market))

    assert reason == "max_spread: side spread 0.200 exceeds 0.050"
    assert ledger.maybe_open_paper_trade(prediction_for(wide_spread_market)) is None


def test_evaluate_paper_exit_flags_take_profit_stop_loss_and_time_exit() -> None:
    config = PaperConfig(take_profit_pct=0.25, stop_loss_pct=-0.30, force_close_seconds_to_close=20)
    now = datetime(2026, 1, 4, 0, 0, tzinfo=UTC)

    take_profit_mark = mark_open_trade_to_market(open_trade(), market(yes_bid=0.55))
    stop_loss_mark = mark_open_trade_to_market(open_trade(), market(yes_bid=0.25))
    time_exit_mark = mark_open_trade_to_market(
        open_trade(), market(yes_bid=0.42, close_time=now + timedelta(seconds=10))
    )

    assert evaluate_paper_exit(open_trade(), take_profit_mark, config, now=now) == "take_profit"
    assert evaluate_paper_exit(open_trade(), stop_loss_mark, config, now=now) == "stop_loss"
    assert evaluate_paper_exit(open_trade(), time_exit_mark, config, now=now) == "time_exit"


def test_close_paper_trade_records_exit_price_reason_and_realized_pnl(tmp_path) -> None:
    ledger = PaperLedger(tmp_path / "paper.sqlite3", PaperConfig())
    insert_open_trade(ledger)

    assert ledger.close_paper_trade("paper-test", exit_price=0.55, exit_reason="take_profit") == 1

    row = ledger.latest_trades(limit=1)[0]
    assert row["status"] == "CLOSED"
    assert row["exit_price"] == 0.55
    assert row["exit_reason"] == "take_profit"
    assert row["realized_pnl"] == pytest.approx(9.375)
    account = ledger.account()
    assert account.open_trades == 0
    assert account.settled_trades == 1
    assert account.cash == pytest.approx(1009.375)


def test_performance_summary_includes_realized_and_open_equity(tmp_path) -> None:
    ledger = PaperLedger(tmp_path / "paper.sqlite3", PaperConfig(initial_cash=1000.0))
    insert_open_trade(ledger)
    with ledger.connect() as conn:
        conn.execute(
            """
            INSERT INTO predictions (
                id, created_at, market_ticker, event_ticker, market_close_time,
                action, side, probability_yes, probability_no, confidence, edge,
                stake_dollars, current_price, target_price, yes_ask, no_ask,
                model_info_json, reasons_json, features_json, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "prediction-loss",
                "2026-01-04T00:15:00+00:00",
                "KXBTC15M-TEST-46",
                "KXBTC15M-TEST",
                "2026-01-04T00:25:00+00:00",
                "BUY_NO",
                "NO",
                0.40,
                0.60,
                0.20,
                0.10,
                25.0,
                100_100.0,
                100_000.0,
                0.50,
                0.50,
                "{}",
                "[]",
                "{}",
                "{}",
            ),
        )
        conn.execute(
            """
            INSERT INTO paper_trades (
                id, prediction_id, created_at, market_ticker, side, entry_price,
                contracts, notional, status, market_close_time, realized_pnl, settled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'SETTLED', ?, ?, ?)
            """,
            (
                "paper-loss",
                "prediction-loss",
                "2026-01-04T00:15:00+00:00",
                "KXBTC15M-TEST-46",
                "NO",
                0.50,
                50.0,
                25.0,
                "2026-01-04T00:25:00+00:00",
                -25.0,
                "2026-01-04T00:26:00+00:00",
            ),
        )
    assert ledger.close_paper_trade("paper-test", exit_price=0.55, exit_reason="take_profit") == 1

    summary = ledger.performance_summary(open_marks=[])

    assert summary["closed_trades"] == 2
    assert summary["winning_trades"] == 1
    assert summary["losing_trades"] == 1
    assert summary["win_rate"] == pytest.approx(0.5)
    assert summary["realized_pnl"] == pytest.approx(-15.625)
    assert summary["expectancy_dollars"] == pytest.approx(-7.8125)
    assert summary["realized_roi_on_risk"] == pytest.approx(-15.625 / 50.0)
    assert summary["total_equity"] == pytest.approx(984.375)
