from __future__ import annotations

from collections.abc import AsyncIterator, Protocol

from .models import OrderBookSnapshot, Tick


class WebsocketFeed(Protocol):
    """Async source of 1-second BTC ticks and Kalshi book snapshots."""

    async def ticks(self) -> AsyncIterator[Tick]: ...

    async def orderbooks(self) -> AsyncIterator[OrderBookSnapshot]: ...


class LiveWebsocketFeed:
    """Placeholder boundary for a future package-local live feed adapter."""

    async def ticks(self) -> AsyncIterator[Tick]:
        raise NotImplementedError("live 1s websocket feed adapter has not been moved into kalshibtc")
        yield  # pragma: no cover

    async def orderbooks(self) -> AsyncIterator[OrderBookSnapshot]:
        raise NotImplementedError("live 1s websocket feed adapter has not been moved into kalshibtc")
        yield  # pragma: no cover
