from __future__ import annotations

from dataclasses import dataclass

from ..market.state import MarketState
from ..strategy.signals import Signal


@dataclass(frozen=True)
class BrokerResult:
    submitted: bool
    reason: str
    order_id: str | None = None


class KalshiBrokerAdapter:
    """Future real-order boundary.

    It is deliberately disabled by default. The modular strategy/risk/backtest
    stack can be exercised without constructing an authenticated order path.
    """

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled

    def submit(self, state: MarketState, signal: Signal) -> BrokerResult:
        del state, signal
        if not self.enabled:
            return BrokerResult(submitted=False, reason="live_orders_disabled")
        return BrokerResult(submitted=False, reason="broker_adapter_not_implemented")
