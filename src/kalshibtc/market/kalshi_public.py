from __future__ import annotations

from typing import Any

import requests


class KalshiPublicClient:
    """Small read-only Kalshi public-data client for the 1s paper path.

    This client only calls unauthenticated market-data endpoints. It has no
    credentials, no order methods, and cannot submit/cancel/amend trades.
    """

    def __init__(self, base_url: str, timeout_seconds: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "kalshibtc-1s-paper-bot/0.1"})

    def get_market(self, ticker: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}/markets/{ticker}",
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        market = payload.get("market", payload)
        return market if isinstance(market, dict) else {}
