from __future__ import annotations

import json
from pathlib import Path

from ..market.contract import ContractWindow
from ..storage.db import connect_sqlite, initialize_schema
from .models import OrderBookSnapshot, Tick


class SnapshotRecorder:
    """SQLite writer for normalized 1 Hz observations.

    This is a datafeed/storage seam only. It does not know about strategies,
    paper fills, or broker adapters.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        initialize_schema(self.path)

    def record_tick_book(
        self,
        *,
        tick: Tick,
        book: OrderBookSnapshot,
        contract: ContractWindow,
        raw_state: dict[str, object] | None = None,
    ) -> None:
        with connect_sqlite(self.path) as conn:
            conn.execute(
                """
                INSERT INTO ticks_1s (
                    ts, symbol, price, bid, ask, source, market_ticker, strike,
                    yes_bid, yes_ask, no_bid, no_ask, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_ticker, ts) DO UPDATE SET
                    price=excluded.price,
                    bid=excluded.bid,
                    ask=excluded.ask,
                    yes_bid=excluded.yes_bid,
                    yes_ask=excluded.yes_ask,
                    no_bid=excluded.no_bid,
                    no_ask=excluded.no_ask,
                    raw_json=excluded.raw_json
                """,
                (
                    tick.ts.isoformat(),
                    tick.symbol,
                    tick.price,
                    tick.bid,
                    tick.ask,
                    tick.source,
                    contract.ticker,
                    contract.strike,
                    book.yes_bid,
                    book.yes_ask,
                    book.no_bid,
                    book.no_ask,
                    json.dumps(raw_state or {}, sort_keys=True),
                ),
            )
