from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from kalshi_btc_15m_bot.models import KalshiMarket, ModelInfo, Prediction
from kalshi_btc_15m_bot.streaming import (
    BtcTick,
    KalshiOrderBook,
    RealtimeStateStreamer,
    build_realtime_state,
    format_realtime_state,
    kalshi_ws_url_from_rest_url,
    parse_binance_book_ticker,
    parse_coinbase_ticker,
)


def market() -> KalshiMarket:
    now = datetime(2026, 1, 4, 0, 0, tzinfo=UTC)
    return KalshiMarket(
        ticker="KXBTC15M-TEST-45",
        event_ticker="KXBTC15M-TEST",
        title="BTC price up in next 15 mins?",
        status="active",
        yes_bid=0.39,
        yes_ask=0.61,
        no_bid=0.38,
        no_ask=0.62,
        last_price=0.50,
        target_price=100_000.0,
        open_time=now,
        close_time=now + timedelta(minutes=10),
        expected_expiration_time=now + timedelta(minutes=15),
        volume=1_000,
        liquidity=200.0,
        open_interest=100,
        raw={},
    )


def prediction(
    test_market: KalshiMarket,
    *,
    probability_yes: float = 0.64,
    action: str = "HOLD",
    stake_dollars: float = 0.0,
    feature_snapshot: dict[str, float] | None = None,
) -> Prediction:
    return Prediction(
        prediction_id="prediction-stream-test",
        created_at=datetime(2026, 1, 4, 0, 5, tzinfo=UTC),
        market=test_market,
        current_price=100_100.0,
        probability_yes=probability_yes,
        probability_no=1.0 - probability_yes,
        action=action,
        side="NO" if action == "BUY_NO" else "YES" if action == "BUY_YES" else None,
        edge=-0.01,
        confidence=0.28,
        stake_dollars=stake_dollars,
        reasons=["unit-test"],
        model_info=ModelInfo(
            name="test-model",
            trained=False,
            samples=0,
            test_accuracy=None,
            brier=None,
            weight=0.0,
        ),
        feature_snapshot=feature_snapshot or {},
    )


def test_kalshi_orderbook_snapshot_and_deltas_drive_top_of_book() -> None:
    book = KalshiOrderBook.from_snapshot(
        "KXBTC15M-TEST-45",
        {
            "market_ticker": "KXBTC15M-TEST-45",
            "yes_dollars_fp": [["0.4000", "10.00"], ["0.5500", "2.00"]],
            "no_dollars_fp": [["0.2500", "4.00"], ["0.3500", "3.00"]],
        },
    )

    assert book.best_yes_bid == pytest.approx(0.55)
    assert book.best_no_bid == pytest.approx(0.35)
    assert book.yes_ask == pytest.approx(0.65)
    assert book.no_ask == pytest.approx(0.45)
    assert book.visible_liquidity == pytest.approx(0.40 * 10 + 0.55 * 2 + 0.25 * 4 + 0.35 * 3)

    book.apply_delta(
        {
            "market_ticker": "KXBTC15M-TEST-45",
            "side": "yes",
            "price_dollars": "0.5600",
            "delta_fp": "5.00",
        }
    )
    assert book.best_yes_bid == pytest.approx(0.56)

    book.apply_delta(
        {
            "market_ticker": "KXBTC15M-TEST-45",
            "side": "yes",
            "price_dollars": "0.5600",
            "delta_fp": "-5.00",
        }
    )
    assert book.best_yes_bid == pytest.approx(0.55)

    book.apply_delta(
        {
            "market_ticker": "KXBTC15M-TEST-45",
            "side": "no",
            "price_dollars": "0.3500",
            "delta_fp": "-3.00",
        }
    )
    assert book.best_no_bid == pytest.approx(0.25)
    assert book.yes_ask == pytest.approx(0.75)


def test_realtime_state_compares_prediction_to_current_orderbook_and_close_clock() -> None:
    test_market = market()
    book = KalshiOrderBook.from_snapshot(
        test_market.ticker,
        {
            "yes_dollars_fp": [["0.5500", "2.00"]],
            "no_dollars_fp": [["0.3500", "3.00"]],
        },
    )
    btc = BtcTick(
        source="binance_ws",
        product="BTCUSDT",
        price=100_100.0,
        bid=100_099.0,
        ask=100_101.0,
        ts=datetime(2026, 1, 4, 0, 5, tzinfo=UTC),
    )

    payload = build_realtime_state(
        prediction(test_market),
        market=test_market,
        orderbook=book,
        btc=btc,
        now=datetime(2026, 1, 4, 0, 5, tzinfo=UTC),
    )

    assert payload["event"] == "market_state"
    assert payload["market_ticker"] == "KXBTC15M-TEST-45"
    assert payload["current_price"] == pytest.approx(100_100.0)
    assert payload["seconds_to_close"] == pytest.approx(300.0)
    assert payload["seconds_to_probability_cutoff"] == pytest.approx(300.0)
    assert payload["probability_cutoff"] == "contract_close"
    assert payload["seconds_to_expiration"] == pytest.approx(600.0)
    assert payload["probability_yes"] == pytest.approx(0.64)
    assert payload["probability_no"] == pytest.approx(0.36)
    assert payload["yes_ask"] == pytest.approx(0.65)
    assert payload["no_ask"] == pytest.approx(0.45)
    assert payload["edge_yes"] == pytest.approx(-0.01)
    assert payload["edge_no"] == pytest.approx(-0.09)
    assert payload["ev_yes_per_contract"] == pytest.approx(-0.01)
    assert payload["ev_no_per_contract"] == pytest.approx(-0.09)
    assert payload["ev_yes_per_dollar"] == pytest.approx(0.64 / 0.65 - 1.0)
    assert payload["ev_no_per_dollar"] == pytest.approx(0.36 / 0.45 - 1.0)
    assert payload["best_ev_side"] == "YES"
    assert payload["best_ev_per_dollar"] == pytest.approx(0.64 / 0.65 - 1.0)
    assert payload["best_side"] == "YES"
    assert payload["best_edge"] == pytest.approx(-0.01)

    formatted = format_realtime_state(payload)
    assert "stream market_state" in formatted
    assert "closes_in=300s" in formatted
    assert "expires_in=" not in formatted
    assert "prob_edge_yes=-0.010" in formatted
    assert "prob_edge_no=-0.090" in formatted
    assert "ev_yes=-1.5%" in formatted
    assert "ev_no=-20.0%" in formatted
    assert "monitor=NO_EDGE ev_side=NONE" in formatted


def test_realtime_state_uses_expiration_aware_probability_for_orderbook_edges() -> None:
    test_market = market()
    book = KalshiOrderBook.from_snapshot(
        test_market.ticker,
        {
            "yes_dollars_fp": [["0.8700", "2.00"]],
            "no_dollars_fp": [["0.1200", "3.00"]],
        },
    )
    btc = BtcTick(
        source="coinbase_ws",
        product="BTC-USD",
        price=100_150.0,
        bid=100_149.0,
        ask=100_151.0,
        ts=datetime(2026, 1, 4, 0, 5, tzinfo=UTC),
    )

    payload = build_realtime_state(
        prediction(
            test_market,
            probability_yes=0.45,
            action="BUY_NO",
            stake_dollars=25.0,
            feature_snapshot={"vol_16": 0.001},
        ),
        market=test_market,
        orderbook=book,
        btc=btc,
        now=datetime(2026, 1, 4, 0, 5, tzinfo=UTC),
    )

    assert payload["direction_state"] == "ABOVE_TARGET"
    assert payload["distance_to_target"] == pytest.approx(150.0)
    assert payload["distance_to_target_pct"] == pytest.approx(0.0015)
    assert payload["model_probability_yes"] == pytest.approx(0.45)
    assert payload["probability_yes"] > 0.85
    assert payload["probability_no"] < 0.15
    assert payload["best_side"] == "YES"
    assert payload["edge_no"] <= 0.0


def test_read_only_stream_format_uses_monitor_language_and_warns_on_direction_disagreement() -> None:
    test_market = market()
    book = KalshiOrderBook.from_snapshot(
        test_market.ticker,
        {
            "yes_dollars_fp": [["0.8700", "2.00"]],
            "no_dollars_fp": [["0.1200", "3.00"]],
        },
    )
    btc = BtcTick(
        source="coinbase_ws",
        product="BTC-USD",
        price=100_020.0,
        bid=100_019.0,
        ask=100_021.0,
        ts=datetime(2026, 1, 4, 0, 5, tzinfo=UTC),
    )

    payload = build_realtime_state(
        prediction(
            test_market,
            probability_yes=0.46,
            action="BUY_NO",
            stake_dollars=25.0,
            feature_snapshot={"vol_16": 0.004},
        ),
        market=test_market,
        orderbook=book,
        btc=btc,
        now=datetime(2026, 1, 4, 0, 5, tzinfo=UTC),
    )

    assert payload["direction_state"] == "ABOVE_TARGET"
    assert payload["best_side"] == "NO"
    assert "edge_direction_disagreement" in payload["warnings"]

    formatted = format_realtime_state(payload)
    assert "monitor=EV_NO" in formatted
    assert "ev_side=NO" in formatted
    assert "prob_edge=" in formatted
    assert "ev_per_$=" in formatted
    assert "paper_action_ref" not in formatted
    assert "paper_stake_ref" not in formatted
    assert "warnings=edge_direction_disagreement" in formatted
    assert "signal=BUY" not in formatted
    assert " stake=$" not in formatted


def test_realtime_state_calculates_ev_dollars_and_watch_only_model_disagreement() -> None:
    now = datetime(2026, 1, 4, 0, 9, tzinfo=UTC)
    base_market = market()
    test_market = KalshiMarket(
        **{
            **base_market.to_jsonable(),
            "close_time": now + timedelta(seconds=60),
            "expected_expiration_time": now + timedelta(minutes=6),
            "raw": {},
        }
    )
    book = KalshiOrderBook.from_snapshot(
        test_market.ticker,
        {
            "yes_dollars_fp": [["0.2200", "2.00"]],
            "no_dollars_fp": [["0.7700", "3.00"]],
        },
    )
    btc = BtcTick(
        source="coinbase_ws",
        product="BTC-USD",
        price=99_903.55,
        bid=99_903.0,
        ask=99_904.0,
        ts=now,
    )

    payload = build_realtime_state(
        prediction(
            test_market,
            probability_yes=0.69,
            action="BUY_YES",
            stake_dollars=25.0,
            feature_snapshot={"vol_16": 0.00386},
        ),
        market=test_market,
        orderbook=book,
        btc=btc,
        now=now,
    )

    assert payload["direction_state"] == "BELOW_TARGET"
    assert payload["probability_yes"] == pytest.approx(0.166, abs=0.002)
    assert payload["probability_no"] == pytest.approx(0.834, abs=0.002)
    assert payload["yes_ask"] == pytest.approx(0.23)
    assert payload["no_ask"] == pytest.approx(0.78)
    assert payload["edge_no"] == pytest.approx(payload["probability_no"] - 0.78)
    assert payload["ev_no_per_contract"] == pytest.approx(payload["edge_no"])
    assert payload["ev_no_per_dollar"] == pytest.approx(payload["probability_no"] / 0.78 - 1.0)
    assert payload["best_ev_side"] == "NO"
    assert payload["best_ev_per_dollar"] == pytest.approx(payload["ev_no_per_dollar"])
    assert payload["best_ev_reference_profit_dollars"] == pytest.approx(
        25.0 * payload["ev_no_per_dollar"]
    )
    assert payload["monitor_action"] == "EV_NO"
    assert payload["decision"] == "WATCH_ONLY_MODEL_DISAGREEMENT"
    assert "model_state_probability_gap" in payload["warnings"]

    formatted = format_realtime_state(payload)
    assert "monitor=EV_NO" in formatted
    assert "ev_side=NO" in formatted
    assert "prob_edge=+0.054" in formatted
    assert "ev_per_$=+6.9%" in formatted
    assert "ev_$25=+$1.72" in formatted
    assert "decision=WATCH_ONLY_MODEL_DISAGREEMENT" in formatted
    assert "paper_action_ref" not in formatted
    assert "paper_stake_ref" not in formatted


def test_realtime_state_marks_positive_ev_below_buffer_as_watch_only_low_ev() -> None:
    test_market = market()
    book = KalshiOrderBook.from_snapshot(
        test_market.ticker,
        {
            "yes_dollars_fp": [["0.2200", "2.00"]],
            "no_dollars_fp": [["0.7700", "3.00"]],
        },
    )
    btc = BtcTick(
        source="coinbase_ws",
        product="BTC-USD",
        price=99_900.0,
        bid=99_899.0,
        ask=99_901.0,
        ts=datetime(2026, 1, 4, 0, 5, tzinfo=UTC),
    )

    payload = build_realtime_state(
        prediction(test_market, probability_yes=0.167),
        market=test_market,
        orderbook=book,
        btc=btc,
        now=datetime(2026, 1, 4, 0, 5, tzinfo=UTC),
    )

    assert payload["best_ev_side"] == "NO"
    assert payload["best_ev_per_dollar"] == pytest.approx(0.833 / 0.78 - 1.0)
    assert payload["monitor_action"] == "EV_NO"
    assert payload["decision"] == "WATCH_ONLY_LOW_EV"


def test_realtime_state_marks_buffered_ev_signal_as_read_only_watch() -> None:
    test_market = market()
    book = KalshiOrderBook.from_snapshot(
        test_market.ticker,
        {
            "yes_dollars_fp": [["0.2200", "2.00"]],
            "no_dollars_fp": [["0.7700", "3.00"]],
        },
    )
    btc = BtcTick(
        source="coinbase_ws",
        product="BTC-USD",
        price=99_900.0,
        bid=99_899.0,
        ask=99_901.0,
        ts=datetime(2026, 1, 4, 0, 5, tzinfo=UTC),
    )

    payload = build_realtime_state(
        prediction(test_market, probability_yes=0.14),
        market=test_market,
        orderbook=book,
        btc=btc,
        now=datetime(2026, 1, 4, 0, 5, tzinfo=UTC),
    )

    assert payload["best_ev_side"] == "NO"
    assert payload["best_ev_per_dollar"] == pytest.approx(0.86 / 0.78 - 1.0)
    assert payload["best_edge"] == pytest.approx(0.08)
    assert payload["best_spread"] == pytest.approx(0.01)
    assert payload["monitor_action"] == "EV_NO"
    assert payload["decision"] == "WATCH_ONLY_EV_SIGNAL"


def test_monitor_action_ignores_dust_edges() -> None:
    test_market = market()
    book = KalshiOrderBook.from_snapshot(
        test_market.ticker,
        {
            "yes_dollars_fp": [["0.6200", "2.00"]],
            "no_dollars_fp": [["0.3700", "3.00"]],
        },
    )
    btc = BtcTick(
        source="coinbase_ws",
        product="BTC-USD",
        price=100_001.0,
        bid=100_000.0,
        ask=100_002.0,
        ts=datetime(2026, 1, 4, 0, 5, tzinfo=UTC),
    )

    payload = build_realtime_state(
        prediction(test_market, probability_yes=0.631),
        market=test_market,
        orderbook=book,
        btc=btc,
        now=datetime(2026, 1, 4, 0, 5, tzinfo=UTC),
    )

    assert payload["best_side"] == "YES"
    assert payload["best_edge"] == pytest.approx(0.001)
    assert payload["monitor_action"] == "NO_EDGE"

    formatted = format_realtime_state(payload)
    assert "monitor=NO_EDGE ev_side=NONE" in formatted


def test_realtime_state_uses_contract_close_for_probability_clock_and_operator_label() -> None:
    test_market = market()
    book = KalshiOrderBook.from_snapshot(
        test_market.ticker,
        {
            "yes_dollars_fp": [["0.7000", "2.00"]],
            "no_dollars_fp": [["0.2400", "3.00"]],
        },
    )
    btc = BtcTick(
        source="coinbase_ws",
        product="BTC-USD",
        price=100_080.0,
        bid=100_079.0,
        ask=100_081.0,
        ts=datetime(2026, 1, 4, 0, 5, tzinfo=UTC),
    )

    payload = build_realtime_state(
        prediction(test_market, probability_yes=0.55, feature_snapshot={"vol_16": 0.002}),
        market=test_market,
        orderbook=book,
        btc=btc,
        now=datetime(2026, 1, 4, 0, 5, tzinfo=UTC),
    )

    assert payload["seconds_to_close"] == pytest.approx(300.0)
    assert payload["seconds_to_probability_cutoff"] == pytest.approx(300.0)
    assert payload["probability_source"] == "close_distance_volatility"
    formatted = format_realtime_state(payload)
    assert "closes_in=300s" in formatted
    assert "expires_in=" not in formatted


def test_invalid_crossed_orderbook_suppresses_monitor_edges() -> None:
    test_market = market()
    book = KalshiOrderBook.from_snapshot(
        test_market.ticker,
        {
            "yes_dollars_fp": [["0.9990", "2.00"]],
            "no_dollars_fp": [["0.1600", "3.00"]],
        },
    )
    btc = BtcTick(
        source="coinbase_ws",
        product="BTC-USD",
        price=100_100.0,
        bid=100_099.0,
        ask=100_101.0,
        ts=datetime(2026, 1, 4, 0, 5, tzinfo=UTC),
    )

    payload = build_realtime_state(
        prediction(test_market, probability_yes=0.93, feature_snapshot={"vol_16": 0.001}),
        market=test_market,
        orderbook=book,
        btc=btc,
        now=datetime(2026, 1, 4, 0, 5, tzinfo=UTC),
    )

    assert payload["orderbook_valid"] is False
    assert payload["edge_yes"] is None
    assert payload["edge_no"] is None
    assert payload["ev_yes_per_dollar"] is None
    assert payload["ev_no_per_dollar"] is None
    assert payload["monitor_action"] == "NO_EDGE"
    assert "invalid_crossed_orderbook" in payload["warnings"]
    formatted = format_realtime_state(payload)
    assert "monitor=NO_EDGE ev_side=NONE" in formatted
    assert "warnings=invalid_crossed_orderbook" in formatted


def test_closed_contract_suppresses_monitor_edges_until_rollover() -> None:
    now = datetime(2026, 1, 4, 0, 10, 1, tzinfo=UTC)
    closed_market = KalshiMarket(
        **{
            **market().to_jsonable(),
            "close_time": datetime(2026, 1, 4, 0, 10, tzinfo=UTC),
            "expected_expiration_time": datetime(2026, 1, 4, 0, 15, tzinfo=UTC),
            "raw": {},
        }
    )
    book = KalshiOrderBook.from_snapshot(
        closed_market.ticker,
        {"yes_dollars_fp": [["0.8300", "2.00"]], "no_dollars_fp": [["0.1600", "3.00"]]},
    )
    btc = BtcTick(
        source="coinbase_ws",
        product="BTC-USD",
        price=100_100.0,
        bid=100_099.0,
        ask=100_101.0,
        ts=now,
    )

    payload = build_realtime_state(
        prediction(closed_market, probability_yes=0.91, feature_snapshot={"vol_16": 0.001}),
        market=closed_market,
        orderbook=book,
        btc=btc,
        now=now,
    )

    assert payload["market_closed"] is True
    assert payload["edge_yes"] is None
    assert payload["edge_no"] is None
    assert payload["ev_yes_per_dollar"] is None
    assert payload["ev_no_per_dollar"] is None
    assert payload["monitor_action"] == "NO_EDGE"
    assert payload["decision"] == "WATCH_ONLY_MARKET_CLOSED"
    assert "market_closed_pending_rollover" in payload["warnings"]


def test_streamer_refreshes_market_after_contract_close() -> None:
    old_market = market()
    new_market = KalshiMarket(
        **{
            **old_market.to_jsonable(),
            "ticker": "KXBTC15M-TEST-60",
            "event_ticker": "KXBTC15M-TEST-NEXT",
            "target_price": 100_250.0,
            "open_time": datetime(2026, 1, 4, 0, 10, tzinfo=UTC),
            "close_time": datetime(2026, 1, 4, 0, 25, tzinfo=UTC),
            "expected_expiration_time": datetime(2026, 1, 4, 0, 30, tzinfo=UTC),
            "raw": {},
        }
    )

    class FakeKalshi:
        def __init__(self) -> None:
            self.market_refreshes = 0

        def current_btc15m_market(self, series_ticker: str, status: str) -> KalshiMarket:
            self.market_refreshes += 1
            assert series_ticker == "KXBTC15M"
            assert status == "open"
            return new_market

        def get_orderbook(self, ticker: str, *, depth: int | None = None) -> dict:
            assert ticker == new_market.ticker
            assert depth == 100
            return {
                "orderbook_fp": {
                    "yes_dollars_fp": [["0.4000", "1.00"]],
                    "no_dollars_fp": [["0.5000", "1.00"]],
                }
            }

    fake_kalshi = FakeKalshi()
    fake_bot = SimpleNamespace(
        kalshi=fake_kalshi,
        config=SimpleNamespace(
            kalshi=SimpleNamespace(series_ticker="KXBTC15M", market_status="open"),
            market_data=SimpleNamespace(request_timeout_seconds=20),
            is_live_mode=False,
        ),
    )
    streamer = RealtimeStateStreamer(fake_bot, emit=lambda _: None)
    streamer.market = old_market
    streamer.orderbook = KalshiOrderBook.from_snapshot(
        old_market.ticker,
        {"yes_dollars_fp": [["0.5000", "1.00"]], "no_dollars_fp": [["0.4000", "1.00"]]},
    )

    refreshed = streamer.refresh_market_if_closed(now=datetime(2026, 1, 4, 0, 10, 1, tzinfo=UTC))

    assert refreshed is True
    assert streamer.market == new_market
    assert streamer.orderbook is not None
    assert streamer.orderbook.market_ticker == new_market.ticker
    assert fake_kalshi.market_refreshes == 1


def test_websocket_url_and_public_btc_ticker_parsers() -> None:
    assert (
        kalshi_ws_url_from_rest_url("https://external-api.kalshi.com/trade-api/v2")
        == "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
    )
    assert (
        kalshi_ws_url_from_rest_url("https://external-api.demo.kalshi.co/trade-api/v2")
        == "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
    )

    binance_tick = parse_binance_book_ticker(
        {"e": "bookTicker", "s": "BTCUSDT", "b": "100.00", "a": "101.00", "E": 1760000000000}
    )
    assert binance_tick.source == "binance_ws"
    assert binance_tick.product == "BTCUSDT"
    assert binance_tick.price == pytest.approx(100.5)

    coinbase_tick = parse_coinbase_ticker(
        {
            "type": "ticker",
            "product_id": "BTC-USD",
            "price": "100.50",
            "best_bid": "100.00",
            "best_ask": "101.00",
            "time": "2026-01-04T00:05:00Z",
        }
    )
    assert coinbase_tick.source == "coinbase_ws"
    assert coinbase_tick.product == "BTC-USD"
    assert coinbase_tick.price == pytest.approx(100.5)
