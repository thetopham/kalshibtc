from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kalshi_btc_15m_bot.config import BotConfig, SupabaseFeatureConfig
from kalshi_btc_15m_bot.supabase_features import (
    SupabaseFeatureClient,
    TradingViewBtcFeatures,
    feature_client_from_config,
    fetch_latest_btc_1m,
)


def _row(ts: datetime) -> dict[str, Any]:
    return {
        "symbol": "BTCUSD",
        "timeframe": 1,
        "ts": ts.isoformat(),
        "o": "100000.0",
        "h": 100250.0,
        "l": 99850.0,
        "c": 100100.0,
        "v": 123.45,
        "sma20": 100010.0,
        "sma50": 99990.0,
        "ema8": 100030.0,
        "ema21": 100020.0,
        "vwap": 100015.0,
        "atr": 120.0,
        "bb_upper": 100500.0,
        "bb_middle": 100000.0,
        "bb_lower": 99500.0,
        "rsi": 56.7,
        "macd": 12.3,
        "macd_signal": 10.0,
        "macd_hist": 2.3,
        "stoch_k": 67.0,
        "stoch_d": 61.0,
        "obv": 123456.0,
        "fisher": 0.42,
        "phobos_momentum": -0.13,
        "vzo": 18.0,
    }


class FakeResponse:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self.payload


class FakeSession:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, *, params: dict[str, Any], timeout: float) -> FakeResponse:
        self.calls.append({"url": url, "params": params, "timeout": timeout, "headers": dict(self.headers)})
        return FakeResponse(self.payload)


def test_supabase_client_fetches_latest_btc_1m_with_expected_rest_query() -> None:
    now = datetime(2026, 5, 14, 17, 1, tzinfo=UTC)
    session = FakeSession([_row(now - timedelta(seconds=12))])
    client = SupabaseFeatureClient(
        url="https://example.supabase.co",
        api_key="anon-test-key",
        table="tv_datafeed_btc",
        symbol="BTCUSD",
        timeframe=1,
        max_feature_age_seconds=90,
        session=session,
    )

    features = client.fetch_latest_btc_1m(now=now, current_price=100_100.0, target_price=100_000.0, seconds_to_close=240.0)

    assert features is not None
    assert features.symbol == "BTCUSD"
    assert features.timeframe == 1
    assert features.c == pytest.approx(100_100.0)
    assert features.atr == pytest.approx(120.0)
    assert features.feature_age_seconds == pytest.approx(12.0)
    call = session.calls[0]
    assert call["url"] == "https://example.supabase.co/rest/v1/tv_datafeed_btc"
    assert call["params"] == {
        "select": "*",
        "symbol": "eq.BTCUSD",
        "timeframe": "eq.1",
        "order": "ts.desc",
        "limit": "1",
    }
    assert call["headers"]["apikey"] == "anon-test-key"
    assert call["headers"]["Authorization"] == "Bearer anon-test-key"


def test_tradingview_features_calculate_freshness_and_atr_target_fields() -> None:
    now = datetime(2026, 5, 14, 17, 1, tzinfo=UTC)
    features = TradingViewBtcFeatures.from_row(_row(now - timedelta(seconds=30))).with_target_context(
        now=now,
        current_price=100_150.0,
        target_price=100_000.0,
        seconds_to_close=240.0,
        max_feature_age_seconds=90,
    )

    assert features.is_fresh is True
    assert features.feature_age_seconds == pytest.approx(30.0)
    assert features.distance_to_target == pytest.approx(150.0)
    assert features.abs_distance_to_target == pytest.approx(150.0)
    assert features.atr_distance == pytest.approx(150.0 / 120.0)
    assert features.atr_to_close_estimate == pytest.approx(120.0 * 2.0)
    assert features.target_z == pytest.approx(150.0 / 240.0)


def test_tradingview_features_mark_stale_when_row_is_too_old() -> None:
    now = datetime(2026, 5, 14, 17, 1, tzinfo=UTC)
    features = fetch_latest_btc_1m(
        SupabaseFeatureClient(
            url="https://example.supabase.co",
            api_key="anon-test-key",
            session=FakeSession([_row(now - timedelta(seconds=120))]),
        ),
        now=now,
        current_price=100_150.0,
        target_price=100_000.0,
        seconds_to_close=240.0,
    )

    assert features is not None
    assert features.feature_age_seconds == pytest.approx(120.0)
    assert features.is_fresh is False
    assert features.to_jsonable()["stale"] is True


def test_feature_client_from_config_builds_enabled_supabase_client() -> None:
    config = BotConfig(
        supabase_features=SupabaseFeatureConfig(
            enabled=True,
            url="https://example.supabase.co",
            api_key="anon-test-key",
            table="tv_datafeed_btc",
            symbol="BTCUSD",
            timeframe=1,
            max_feature_age_seconds=45.0,
            request_timeout_seconds=4.0,
        )
    )

    client = feature_client_from_config(config)

    assert client is not None
    assert client.url == "https://example.supabase.co"
    assert client.table == "tv_datafeed_btc"
    assert client.symbol == "BTCUSD"
    assert client.timeframe == 1
    assert client.max_feature_age_seconds == pytest.approx(45.0)


def test_tradingview_features_export_model_snapshot_with_staleness_gate() -> None:
    now = datetime(2026, 5, 14, 17, 1, tzinfo=UTC)
    features = TradingViewBtcFeatures.from_row(_row(now - timedelta(seconds=120))).with_target_context(
        now=now,
        current_price=100_150.0,
        target_price=100_000.0,
        seconds_to_close=240.0,
        max_feature_age_seconds=90,
    )

    snapshot = features.to_feature_snapshot()

    assert snapshot["tv_atr"] == pytest.approx(120.0)
    assert snapshot["tv_distance_to_target"] == pytest.approx(150.0)
    assert snapshot["tv_stale"] == pytest.approx(1.0)
