from __future__ import annotations

from dataclasses import replace
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
        return self._with_orderbook_quote(KalshiMarket.from_api(raw))

    def get_orderbook(self, ticker: str, *, depth: int | None = None) -> dict[str, Any]:
        params = {"depth": depth} if depth is not None else None
        return self._get(f"/markets/{ticker}/orderbook", params=params)

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
            return self._with_orderbook_quote(min(live, key=lambda m: m.close_time or now))

        upcoming_or_recent = [m for m in markets if m.close_time is not None]
        if upcoming_or_recent:
            return self._with_orderbook_quote(
                min(upcoming_or_recent, key=lambda m: abs((m.close_time or now) - now))
            )
        return self._with_orderbook_quote(markets[0])

    def _with_orderbook_quote(self, market: KalshiMarket) -> KalshiMarket:
        """Refresh top-of-book quotes from Kalshi's orderbook when available.

        The /markets payload usually includes bid/ask fields, but the orderbook
        is the source of truth for currently resting bids. Kalshi returns YES
        bids and NO bids; asks are complements of the opposite side's best bid.
        If the orderbook is unavailable, keep the /markets quote and fail open
        for read-only quoting instead of breaking status/scan output.
        """
        try:
            payload = self.get_orderbook(market.ticker, depth=20)
        except requests.RequestException:
            return market
        orderbook = payload.get("orderbook_fp") or payload.get("orderbook") or {}
        yes_bid = _best_bid(orderbook.get("yes_dollars") or orderbook.get("yes"))
        no_bid = _best_bid(orderbook.get("no_dollars") or orderbook.get("no"))
        if yes_bid is None and no_bid is None:
            return market
        refreshed_yes_bid = yes_bid if yes_bid is not None else market.yes_bid
        refreshed_no_bid = no_bid if no_bid is not None else market.no_bid
        refreshed_yes_ask = (1.0 - refreshed_no_bid) if no_bid is not None else market.yes_ask
        refreshed_no_ask = (1.0 - refreshed_yes_bid) if yes_bid is not None else market.no_ask
        visible_liquidity = _visible_liquidity(orderbook.get("yes_dollars") or orderbook.get("yes"))
        visible_liquidity += _visible_liquidity(orderbook.get("no_dollars") or orderbook.get("no"))
        return replace(
            market,
            yes_bid=round(refreshed_yes_bid, 4),
            yes_ask=round(refreshed_yes_ask, 4),
            no_bid=round(refreshed_no_bid, 4),
            no_ask=round(refreshed_no_ask, 4),
            liquidity=round(max(market.liquidity, visible_liquidity), 4),
        )


def _is_live_window(market: KalshiMarket, now: datetime) -> bool:
    if not market.open_time or not market.close_time:
        return False
    return market.open_time <= now < market.close_time


def _best_bid(levels: Any) -> float | None:
    best: float | None = None
    if not isinstance(levels, list):
        return None
    for level in levels:
        if not isinstance(level, list | tuple) or not level:
            continue
        try:
            price = float(level[0])
        except (TypeError, ValueError):
            continue
        if 0.0 < price < 1.0 and (best is None or price > best):
            best = price
    return best


def _visible_liquidity(levels: Any) -> float:
    total = 0.0
    if not isinstance(levels, list):
        return total
    for level in levels:
        if not isinstance(level, list | tuple) or len(level) < 2:
            continue
        try:
            price = float(level[0])
            count = float(level[1])
        except (TypeError, ValueError):
            continue
        if 0.0 < price < 1.0 and count > 0.0:
            total += price * count
    return total
