from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_btc_15m_bot.config import BotConfig, LiveConfig
from kalshi_btc_15m_bot.live import (
    KalshiAuthenticatedClient,
    KalshiCredentialError,
    LiveLedger,
    LiveTrader,
)
from kalshi_btc_15m_bot.models import KalshiMarket, ModelInfo, Prediction


def write_private_key(path: Path, *, mode: int = 0o600):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    path.chmod(mode)
    return key


def market(
    *,
    yes_bid: float = 0.39,
    yes_ask: float = 0.40,
    no_bid: float = 0.59,
    no_ask: float = 0.60,
    liquidity: float = 250.0,
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
        last_price=yes_bid,
        target_price=100_000.0,
        open_time=now - timedelta(minutes=5),
        close_time=close_time or now + timedelta(minutes=10),
        expected_expiration_time=now + timedelta(minutes=15),
        volume=1_000,
        liquidity=liquidity,
        open_interest=100,
        raw={},
    )


def prediction_for(test_market: KalshiMarket) -> Prediction:
    return Prediction(
        prediction_id="prediction-live-test",
        created_at=datetime(2026, 1, 4, 0, 0, tzinfo=UTC),
        market=test_market,
        current_price=100_100.0,
        probability_yes=0.65,
        probability_no=0.35,
        action="BUY_YES",
        side="YES",
        edge=0.25,
        confidence=0.30,
        stake_dollars=25.0,
        reasons=["unit-test"],
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


def live_config(tmp_path: Path, **overrides: Any) -> BotConfig:
    live = LiveConfig(
        environment="demo",
        base_url="https://external-api.demo.kalshi.co/trade-api/v2",
        acknowledgement="I_UNDERSTAND_KALSHI_DEMO_ORDERS",
        auto_trade=True,
        max_order_dollars=5.0,
        max_contracts=5,
        max_open_positions=1,
        max_daily_orders=3,
        max_daily_loss_dollars=10.0,
        min_cash_reserve_dollars=25.0,
        min_liquidity_dollars=50.0,
        max_spread=0.10,
        take_profit_pct=0.20,
        stop_loss_pct=-0.30,
        force_close_seconds_to_close=30,
        min_seconds_between_orders=60,
        time_in_force="immediate_or_cancel",
        subaccount=0,
    )
    if overrides:
        live = replace(live, **overrides)
    return replace(
        BotConfig(),
        trading_mode="live",
        enable_live_orders=True,
        data_dir=tmp_path,
        live=live,
    )


class FakeLiveClient:
    def __init__(
        self,
        *,
        balance_cents: int = 5_000,
        positions: list[dict[str, Any]] | None = None,
        fills: list[dict[str, Any]] | None = None,
        fail_positions: bool = False,
        fail_fills: bool = False,
    ) -> None:
        self.balance_cents = balance_cents
        self.positions = positions or []
        self.fills = fills or []
        self.fail_positions = fail_positions
        self.fail_fills = fail_fills
        self.created_orders: list[dict[str, Any]] = []

    def get_balance(self, *, subaccount: int = 0) -> dict[str, Any]:
        return {"balance": self.balance_cents, "portfolio_value": self.balance_cents, "subaccount": subaccount}

    def create_order(self, body: dict[str, Any]) -> dict[str, Any]:
        self.created_orders.append(body)
        return {"order": {"order_id": f"ord-{len(self.created_orders)}", "status": "accepted", **body}}

    def list_fills(self, **_: Any) -> list[dict[str, Any]]:
        if self.fail_fills:
            raise RuntimeError("fills unavailable")
        return self.fills

    def list_positions(self, **_: Any) -> list[dict[str, Any]]:
        if self.fail_positions:
            raise RuntimeError("positions unavailable")
        return self.positions


def test_authenticated_client_signs_full_api_path_without_query(tmp_path) -> None:
    key_path = tmp_path / "kalshi.key"
    private_key = write_private_key(key_path)
    client = KalshiAuthenticatedClient(
        base_url="https://external-api.demo.kalshi.co/trade-api/v2",
        api_key_id="key-id",
        private_key_file=key_path,
        timestamp_ms=lambda: "1703123456789",
    )

    headers = client.auth_headers("GET", "/portfolio/orders?limit=5")

    signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    private_key.public_key().verify(
        signature,
        b"1703123456789GET/trade-api/v2/portfolio/orders",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    assert headers["KALSHI-ACCESS-KEY"] == "key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1703123456789"


def test_authenticated_client_rejects_group_readable_private_key(tmp_path) -> None:
    key_path = tmp_path / "kalshi.key"
    write_private_key(key_path, mode=0o644)

    with pytest.raises(KalshiCredentialError, match="chmod 600"):
        KalshiAuthenticatedClient(
            base_url="https://external-api.demo.kalshi.co/trade-api/v2",
            api_key_id="key-id",
            private_key_file=key_path,
        )


def test_live_ledger_records_orders_fills_and_reconstructs_open_position(tmp_path) -> None:
    ledger = LiveLedger(tmp_path / "ledger.sqlite3")
    ledger.record_order_intent(
        client_order_id="coid-1",
        prediction_id="prediction-live-test",
        market_ticker="KXBTC15M-TEST-45",
        side="YES",
        action="buy",
        count=2,
        limit_price_cents=40,
        max_cost_cents=80,
        time_in_force="immediate_or_cancel",
        request={"ticker": "KXBTC15M-TEST-45"},
    )
    ledger.record_order_success("coid-1", {"order": {"order_id": "ord-1", "status": "filled"}})
    ledger.record_fills(
        [
            {
                "fill_id": "fill-buy",
                "order_id": "ord-1",
                "trade_id": "trade-1",
                "market_ticker": "KXBTC15M-TEST-45",
                "side": "yes",
                "action": "buy",
                "count_fp": "2.00",
                "yes_price_dollars": "0.4000",
                "fee_cost": "0.0000",
                "created_time": "2026-01-04T00:00:01Z",
            },
            {
                "fill_id": "fill-sell",
                "order_id": "ord-2",
                "trade_id": "trade-2",
                "market_ticker": "KXBTC15M-TEST-45",
                "side": "yes",
                "action": "sell",
                "count_fp": "1.00",
                "yes_price_dollars": "0.6000",
                "fee_cost": "0.0000",
                "created_time": "2026-01-04T00:02:01Z",
            },
        ]
    )

    positions = ledger.position_summaries()

    assert len(positions) == 1
    assert positions[0]["market_ticker"] == "KXBTC15M-TEST-45"
    assert positions[0]["side"] == "YES"
    assert positions[0]["count"] == pytest.approx(1.0)
    assert positions[0]["avg_entry_price"] == pytest.approx(0.40)
    assert positions[0]["realized_pnl"] == pytest.approx(0.20)
    assert ledger.realized_pnl() == pytest.approx(0.20)
    assert ledger.daily_realized_pnl(datetime(2026, 1, 4, 1, 0, tzinfo=UTC)) == pytest.approx(0.20)


def test_live_ledger_keeps_realized_pnl_after_position_is_fully_closed(tmp_path) -> None:
    ledger = LiveLedger(tmp_path / "ledger.sqlite3")
    ledger.record_fills(
        [
            {
                "fill_id": "fill-buy",
                "order_id": "ord-1",
                "trade_id": "trade-1",
                "market_ticker": "KXBTC15M-TEST-45",
                "side": "yes",
                "action": "buy",
                "count_fp": "2.00",
                "yes_price_dollars": "0.4000",
                "fee_cost": "0.0000",
                "created_time": "2026-01-04T00:00:01Z",
            },
            {
                "fill_id": "fill-sell-all",
                "order_id": "ord-2",
                "trade_id": "trade-2",
                "market_ticker": "KXBTC15M-TEST-45",
                "side": "yes",
                "action": "sell",
                "count_fp": "2.00",
                "yes_price_dollars": "0.2500",
                "fee_cost": "0.0000",
                "created_time": "2026-01-04T00:02:01Z",
            },
        ]
    )

    assert ledger.position_summaries() == []
    assert ledger.realized_pnl() == pytest.approx(-0.30)
    assert ledger.daily_realized_pnl(datetime(2026, 1, 4, 1, 0, tzinfo=UTC)) == pytest.approx(-0.30)


def test_live_ledger_daily_realized_pnl_uses_full_cross_day_cost_basis(tmp_path) -> None:
    ledger = LiveLedger(tmp_path / "ledger.sqlite3")
    ledger.record_fills(
        [
            {
                "fill_id": "fill-buy-yesterday",
                "order_id": "ord-1",
                "trade_id": "trade-1",
                "market_ticker": "KXBTC15M-TEST-45",
                "side": "yes",
                "action": "buy",
                "count_fp": "10.00",
                "yes_price_dollars": "0.9000",
                "fee_cost": "0.0000",
                "created_time": "2026-01-03T23:59:00Z",
            },
            {
                "fill_id": "fill-sell-today",
                "order_id": "ord-2",
                "trade_id": "trade-2",
                "market_ticker": "KXBTC15M-TEST-45",
                "side": "yes",
                "action": "sell",
                "count_fp": "10.00",
                "yes_price_dollars": "0.1000",
                "fee_cost": "0.0000",
                "created_time": "2026-01-04T00:01:00Z",
            },
        ]
    )

    assert ledger.realized_pnl() == pytest.approx(-8.0)
    assert ledger.daily_realized_pnl(datetime(2026, 1, 3, 12, 0, tzinfo=UTC)) == pytest.approx(0.0)
    assert ledger.daily_realized_pnl(datetime(2026, 1, 4, 12, 0, tzinfo=UTC)) == pytest.approx(-8.0)


def test_live_trader_submits_ioc_limit_buy_with_caps_and_balance_reserve(tmp_path) -> None:
    fake_client = FakeLiveClient(balance_cents=5_000)
    ledger = LiveLedger(tmp_path / "ledger.sqlite3")
    trader = LiveTrader(live_config(tmp_path), client=fake_client, ledger=ledger)

    result = trader.maybe_submit_entry(prediction_for(market()))

    assert result["submitted"] is True
    body = fake_client.created_orders[0]
    assert body["ticker"] == "KXBTC15M-TEST-45"
    assert body["side"] == "yes"
    assert body["action"] == "buy"
    assert body["count"] == 5
    assert body["yes_price"] == 40
    assert body["time_in_force"] == "immediate_or_cancel"
    assert body["buy_max_cost"] == 200
    assert body["post_only"] is False
    assert ledger.latest_orders(limit=1)[0]["status"] == "accepted"


def test_live_trader_blocks_entry_when_cash_reserve_would_be_breached(tmp_path) -> None:
    fake_client = FakeLiveClient(balance_cents=2_600)
    trader = LiveTrader(live_config(tmp_path), client=fake_client, ledger=LiveLedger(tmp_path / "ledger.sqlite3"))

    result = trader.maybe_submit_entry(prediction_for(market()))

    assert result["submitted"] is False
    assert "min_cash_reserve" in result["reason"]
    assert fake_client.created_orders == []


def test_live_trader_blocks_entry_when_remote_position_exists_before_local_fill_sync(tmp_path) -> None:
    fake_client = FakeLiveClient(
        balance_cents=5_000,
        positions=[{"ticker": "KXBTC15M-TEST-45", "position": 1}],
    )
    trader = LiveTrader(live_config(tmp_path), client=fake_client, ledger=LiveLedger(tmp_path / "ledger.sqlite3"))

    result = trader.maybe_submit_entry(prediction_for(market()))

    assert result["submitted"] is False
    assert result["reason"] == "max_open_positions: reached 1"
    assert fake_client.created_orders == []


def test_live_trader_fails_closed_when_remote_position_reconciliation_fails(tmp_path) -> None:
    fake_client = FakeLiveClient(balance_cents=5_000, fail_positions=True)
    trader = LiveTrader(live_config(tmp_path), client=fake_client, ledger=LiveLedger(tmp_path / "ledger.sqlite3"))

    result = trader.maybe_submit_entry(prediction_for(market()))

    assert result["submitted"] is False
    assert result["reason"].startswith("remote_position_reconciliation_failed")
    assert fake_client.created_orders == []


def test_live_trader_uses_unique_exit_client_order_ids(tmp_path) -> None:
    fake_client = FakeLiveClient(balance_cents=5_000)
    trader = LiveTrader(live_config(tmp_path), client=fake_client, ledger=LiveLedger(tmp_path / "ledger.sqlite3"))
    position = {
        "market_ticker": "KXBTC15M-TEST-45",
        "side": "YES",
        "count": 2.0,
        "cost_basis": 0.80,
        "avg_entry_price": 0.40,
    }

    first = trader._exit_order_plan(position, market(yes_bid=0.60), "take_profit")
    second = trader._exit_order_plan(position, market(yes_bid=0.60), "take_profit")

    assert first.client_order_id != second.client_order_id
    assert first.body["reduce_only"] is True
    assert second.body["reduce_only"] is True


def test_live_trader_submits_reduce_only_exit_on_take_profit(tmp_path) -> None:
    fake_client = FakeLiveClient(balance_cents=5_000)
    ledger = LiveLedger(tmp_path / "ledger.sqlite3")
    ledger.record_fills(
        [
            {
                "fill_id": "fill-buy",
                "order_id": "ord-1",
                "trade_id": "trade-1",
                "market_ticker": "KXBTC15M-TEST-45",
                "side": "yes",
                "action": "buy",
                "count_fp": "2.00",
                "yes_price_dollars": "0.4000",
                "fee_cost": "0.0000",
                "created_time": "2026-01-04T00:00:01Z",
            }
        ]
    )
    trader = LiveTrader(live_config(tmp_path), client=fake_client, ledger=ledger)

    managed = trader.manage_open_positions(lambda _: market(yes_bid=0.60, yes_ask=0.62))

    assert managed[0]["exit_signal"] == "take_profit"
    assert managed[0]["submitted"] is True
    body = fake_client.created_orders[0]
    assert body["action"] == "sell"
    assert body["side"] == "yes"
    assert body["count"] == 2
    assert body["yes_price"] == 60
    assert body["reduce_only"] is True
