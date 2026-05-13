from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

from .models import KalshiMarket, now_utc


class KalshiPublicClient:
    """Small public-data Kalshi client.

    This client intentionally uses only unauthenticated market-data endpoints. It
    cannot place, amend, or cancel orders.
    """

    def __init__(self, base_url: str, timeout_seconds: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "kalshi-btc-15m-paper-bot/0.1"})

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}{path}", params=params, timeout=self.timeout_seconds
        )
        response.raise_for_status()
        return response.json()

    def list_markets(
        self,
        *,
        series_ticker: str,
        status: str | None = "open",
        limit: int = 100,
    ) -> list[KalshiMarket]:
        params: dict[str, Any] = {"series_ticker": series_ticker, "limit": limit}
        if status:
            params["status"] = status
        payload = self._get("/markets", params=params)
        return [KalshiMarket.from_api(item) for item in payload.get("markets", [])]

    def get_market(self, ticker: str) -> KalshiMarket:
        payload = self._get(f"/markets/{ticker}")
        raw = payload.get("market", payload)
        return KalshiMarket.from_api(raw)

    def get_orderbook(self, ticker: str) -> dict[str, Any]:
        return self._get(f"/markets/{ticker}/orderbook")

    def current_btc15m_market(self, series_ticker: str, status: str = "open") -> KalshiMarket:
        markets = self.list_markets(series_ticker=series_ticker, status=status, limit=50)
        if not markets:
            # Kalshi sometimes labels live filtered markets as status=active while
            # the query parameter is status=open. Try an unfiltered list before failing.
            markets = self.list_markets(series_ticker=series_ticker, status=None, limit=50)
        if not markets:
            raise LookupError(f"No markets found for series {series_ticker}")

        now = now_utc()
        live = [m for m in markets if _is_live_window(m, now)]
        if live:
            return min(live, key=lambda m: m.close_time or now)

        upcoming_or_recent = [m for m in markets if m.close_time is not None]
        if upcoming_or_recent:
            return min(upcoming_or_recent, key=lambda m: abs((m.close_time or now) - now))
        return markets[0]


def _is_live_window(market: KalshiMarket, now: datetime) -> bool:
    if not market.open_time or not market.close_time:
        return False
    return market.open_time <= now < market.close_time
