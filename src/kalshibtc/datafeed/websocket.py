from __future__ import annotations

from collections.abc import AsyncIterator, Protocol

from .models import OrderBookSnapshot, Tick


class WebsocketFeed(Protocol):
    """Async source of 1-second BTC ticks and Kalshi book snapshots."""

    async def ticks(self) -> AsyncIterator[Tick]: ...

    async def orderbooks(self) -> AsyncIterator[OrderBookSnapshot]: ...


class LiveWebsocketFeed:
    """Placeholder adapter boundary for the existing websocket implementation.

    The current production websocket loop still lives in
    `kalshi_btc_15m_bot.streaming`. New strategy, replay, risk, and execution
    code should depend on the `WebsocketFeed` protocol above so the old stream
    can be wrapped without leaking strategy logic into transport code.
    """

    async def ticks(self) -> AsyncIterator[Tick]:
        raise NotImplementedError("wrap kalshi_btc_15m_bot.streaming before using live feed")
        yield  # pragma: no cover

    async def orderbooks(self) -> AsyncIterator[OrderBookSnapshot]:
        raise NotImplementedError("wrap kalshi_btc_15m_bot.streaming before using live feed")
        yield  # pragma: no cover
