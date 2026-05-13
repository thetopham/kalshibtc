from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from kalshi_btc_15m_bot.kalshi_client import KalshiPublicClient


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, url: str, params: dict | None = None, timeout: int | None = None) -> FakeResponse:
        self.calls.append((url, params))
        parsed = urlparse(url)
        if parsed.path.endswith("/markets"):
            return FakeResponse(
                {
                    "markets": [
                        {
                            "ticker": "KXBTC15M-TEST-45",
                            "event_ticker": "KXBTC15M-TEST",
                            "title": "BTC price up in next 15 mins?",
                            "status": "active",
                            "yes_bid_dollars": "0.2000",
                            "yes_ask_dollars": "0.8000",
                            "no_bid_dollars": "0.1000",
                            "no_ask_dollars": "0.9000",
                            "last_price_dollars": "0.5000",
                            "floor_strike": "100000",
                            "open_time": "2026-01-04T00:00:00Z",
                            "close_time": "2099-01-04T00:15:00Z",
                            "expected_expiration_time": "2099-01-04T00:15:00Z",
                            "volume_fp": "100.00",
                            "liquidity_dollars": "0.0000",
                            "open_interest_fp": "10.00",
                        }
                    ]
                }
            )
        if parsed.path.endswith("/markets/KXBTC15M-TEST-45/orderbook"):
            assert parse_qs(parsed.query) == {}
            assert params == {"depth": 20}
            return FakeResponse(
                {
                    "orderbook_fp": {
                        "yes_dollars": [["0.2300", "10.00"], ["0.5500", "2.00"]],
                        "no_dollars": [["0.0100", "10.00"], ["0.4200", "3.00"]],
                    }
                }
            )
        raise AssertionError(f"unexpected url {url}")


def test_current_market_refreshes_best_quotes_from_kalshi_orderbook() -> None:
    client = KalshiPublicClient("https://external-api.kalshi.com/trade-api/v2")
    fake_session = FakeSession()
    client.session = fake_session  # type: ignore[assignment]

    market = client.current_btc15m_market("KXBTC15M", "open")

    assert market.yes_bid == 0.55
    assert market.no_bid == 0.42
    assert market.yes_ask == 0.58
    assert market.no_ask == 0.45
    assert market.liquidity == 0.55 * 2.0 + 0.23 * 10.0 + 0.42 * 3.0 + 0.01 * 10.0
    assert fake_session.calls[0][0].endswith("/markets")
    assert fake_session.calls[1][0].endswith("/markets/KXBTC15M-TEST-45/orderbook")
