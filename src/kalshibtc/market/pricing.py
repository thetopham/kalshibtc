from __future__ import annotations

from ..datafeed.models import OrderBookSnapshot


def entry_price_for_signal(side: str, book: OrderBookSnapshot) -> float | None:
    if side == "long_above":
        return book.yes_ask
    if side == "long_below":
        return book.no_ask
    return None


def bid_price_for_signal(side: str, book: OrderBookSnapshot) -> float | None:
    if side == "long_above":
        return book.yes_bid
    if side == "long_below":
        return book.no_bid
    return None


def spread_for_signal(side: str, book: OrderBookSnapshot) -> float | None:
    entry = entry_price_for_signal(side, book)
    bid = bid_price_for_signal(side, book)
    if entry is None or bid is None:
        return None
    return entry - bid


def book_is_valid(book: OrderBookSnapshot) -> bool:
    prices = (book.yes_bid, book.yes_ask, book.no_bid, book.no_ask)
    return all(price is not None and 0.0 < price < 1.0 for price in prices) and not book.is_crossed
