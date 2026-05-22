from __future__ import annotations

from dataclasses import dataclass, field

from ..execution.paper import PaperFill
from ..market.state import MarketState
from .signals import Signal


@dataclass(frozen=True)
class LateLottoTicketConfig:
    """Buy cheap one-sided lottery tickets shortly before Kalshi BTC expiry.

    This is intentionally a single-position directional strategy. It buys the
    cheapest YES/NO ask inside a narrow final-window band and then blocks repeat
    entries in the same market. It is for replay research of cheap-tail behavior,
    not a paired hedge or inventory repair strategy.
    """

    name: str = "late_lotto_ticket"
    min_seconds_to_close: float = 60.0
    max_seconds_to_close: float = 120.0
    max_ticket_price: float = 0.02
    min_ticket_price: float = 0.001
    base_notional: float = 10.0
    side_mode: str = "cheapest"


@dataclass
class LateLottoTicketStrategy:
    config: LateLottoTicketConfig = field(default_factory=LateLottoTicketConfig)

    def __post_init__(self) -> None:
        self._active_market_ticker: str | None = None
        self._position_side: str | None = None

    @property
    def name(self) -> str:
        return self.config.name

    def on_tick(self, state: MarketState) -> Signal:
        if self._active_market_ticker != state.contract.ticker:
            self._active_market_ticker = state.contract.ticker
            self._position_side = None

        seconds = float(state.seconds_to_close)
        if seconds < self.config.min_seconds_to_close or seconds > self.config.max_seconds_to_close:
            return self._none("outside_lotto_window", state)
        if self._position_side is not None:
            return self._none("late_lotto_ticket_already_open", state)

        yes_ask = state.orderbook.yes_ask
        no_ask = state.orderbook.no_ask
        side_mode = self.config.side_mode.lower()
        if side_mode == "yes_only":
            candidates: list[tuple[str, str, float | None]] = [("long_above", "yes", yes_ask)]
        elif side_mode == "no_only":
            candidates = [("long_below", "no", no_ask)]
        else:
            candidates = [
                ("long_above", "yes", yes_ask),
                ("long_below", "no", no_ask),
            ]
        valid = [
            (side, label, float(price))
            for side, label, price in candidates
            if price is not None
            and self.config.min_ticket_price <= float(price) <= self.config.max_ticket_price
        ]
        if not valid:
            return self._none("no_lotto_ticket_price", state)

        side, label, price = min(valid, key=lambda item: (item[2], 0 if item[1] == "no" else 1))
        shares = self.config.base_notional / price
        return Signal(
            side=side,
            reason=f"late_lotto_ticket_cheapest_{label}",
            confidence=0.50,
            strategy=self.name,
            target_notional=self.config.base_notional,
            estimated_shares=shares,
            features=self._features(state, label=label, price=price),
            allow_price_strike_mismatch=True,
        )

    def on_fill(self, state: MarketState, fill: PaperFill) -> None:
        side = "yes" if fill.side == "long_above" else "no" if fill.side == "long_below" else None
        if side is None:
            return
        if self._active_market_ticker != state.contract.ticker:
            self._active_market_ticker = state.contract.ticker
        self._position_side = side

    def _none(self, reason: str, state: MarketState) -> Signal:
        return Signal(
            "none",
            reason,
            0.0,
            strategy=self.name,
            features=self._features(state, label=None, price=None),
            allow_price_strike_mismatch=True,
        )

    def _features(self, state: MarketState, *, label: str | None, price: float | None) -> dict[str, float | str | None]:
        return {
            "lotto_ticket_side": label,
            "lotto_ticket_price": price,
            "seconds_to_close": float(state.seconds_to_close),
            "distance_to_strike": float(state.price - state.strike),
            "yes_ask": state.orderbook.yes_ask,
            "no_ask": state.orderbook.no_ask,
            "max_ticket_price": float(self.config.max_ticket_price),
            "min_ticket_price": float(self.config.min_ticket_price),
            "side_mode": self.config.side_mode,
        }
