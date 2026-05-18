from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import requests

POLYMARKET_CLOB_URL = "https://clob.polymarket.com"


class HTTPSession(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class PolymarketBookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class PolymarketOrderBook:
    token_id: str
    market: str | None
    timestamp: str | None
    bids: tuple[PolymarketBookLevel, ...]
    asks: tuple[PolymarketBookLevel, ...]
    min_order_size: float | None
    tick_size: float | None
    hash: str | None
    raw: dict[str, Any]

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


@dataclass(frozen=True)
class FillValidationObservation:
    ts: str
    token_id: str
    side: str
    limit_price: float
    contracts: float
    best_bid: float | None
    best_ask: float | None
    spread: float | None
    min_order_size: float | None
    tick_size: float | None
    replay_touch: bool
    fillable_contracts_at_limit: float
    fillable_notional_at_limit: float
    fully_fillable_at_limit: bool
    partial_fill_expected: bool
    top_level_size: float | None
    book_hash: str | None


@dataclass(frozen=True)
class FillValidationSummary:
    token_id: str
    side: str
    limit_price: float
    contracts: float
    observations: int
    replay_touches: int
    full_fill_observations: int
    partial_fill_observations: int
    min_order_size_blocks: int
    touch_rate: float
    full_fill_rate: float
    partial_fill_rate: float
    replay_overstates_full_fill_count: int
    avg_fillable_contracts_when_touched: float | None


class PolymarketPublicClient:
    def __init__(self, *, base_url: str = POLYMARKET_CLOB_URL, session: HTTPSession | None = None, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout

    def get_order_book(self, token_id: str) -> PolymarketOrderBook:
        response = self.session.get(f"{self.base_url}/book", params={"token_id": token_id}, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Polymarket /book response was not a JSON object")
        return parse_order_book(token_id=token_id, payload=payload)


def parse_order_book(*, token_id: str, payload: dict[str, Any]) -> PolymarketOrderBook:
    return PolymarketOrderBook(
        token_id=str(payload.get("asset_id") or token_id),
        market=_str_or_none(payload.get("market")),
        timestamp=_str_or_none(payload.get("timestamp")),
        bids=_levels(payload.get("bids"), reverse=True),
        asks=_levels(payload.get("asks"), reverse=False),
        min_order_size=_float_or_none(payload.get("min_order_size")),
        tick_size=_float_or_none(payload.get("tick_size")),
        hash=_str_or_none(payload.get("hash")),
        raw=payload,
    )


def observe_fillability(*, book: PolymarketOrderBook, side: str, limit_price: float, contracts: float, now: datetime | None = None) -> FillValidationObservation:
    side = side.upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if contracts <= 0:
        raise ValueError("contracts must be positive")
    if not (0.0 < limit_price < 1.0):
        raise ValueError("limit_price must be between 0 and 1")

    levels = book.asks if side == "BUY" else book.bids
    if side == "BUY":
        fillable_levels = [level for level in levels if level.price <= limit_price]
        replay_touch = book.best_ask is not None and book.best_ask <= limit_price
        top_level_size = book.asks[0].size if book.asks else None
    else:
        fillable_levels = [level for level in levels if level.price >= limit_price]
        replay_touch = book.best_bid is not None and book.best_bid >= limit_price
        top_level_size = book.bids[0].size if book.bids else None

    fillable_contracts = sum(level.size for level in fillable_levels)
    fillable_notional = sum(level.size * level.price for level in fillable_levels)
    min_order_size = book.min_order_size
    min_order_block = min_order_size is not None and contracts < min_order_size
    fully_fillable = (not min_order_block) and fillable_contracts >= contracts
    partial_fill = (not min_order_block) and 0 < fillable_contracts < contracts
    ts = (now or datetime.now(tz=UTC)).astimezone(UTC).isoformat()
    return FillValidationObservation(
        ts=ts,
        token_id=book.token_id,
        side=side,
        limit_price=limit_price,
        contracts=contracts,
        best_bid=book.best_bid,
        best_ask=book.best_ask,
        spread=book.spread,
        min_order_size=book.min_order_size,
        tick_size=book.tick_size,
        replay_touch=replay_touch,
        fillable_contracts_at_limit=fillable_contracts,
        fillable_notional_at_limit=fillable_notional,
        fully_fillable_at_limit=fully_fillable,
        partial_fill_expected=partial_fill,
        top_level_size=top_level_size,
        book_hash=book.hash,
    )


def summarize_observations(observations: list[FillValidationObservation]) -> FillValidationSummary:
    if not observations:
        raise ValueError("cannot summarize zero observations")
    first = observations[0]
    touches = [obs for obs in observations if obs.replay_touch]
    full = [obs for obs in observations if obs.fully_fillable_at_limit]
    partial = [obs for obs in observations if obs.partial_fill_expected]
    min_blocks = [obs for obs in observations if obs.min_order_size is not None and obs.contracts < obs.min_order_size]
    avg_fillable = None
    if touches:
        avg_fillable = sum(obs.fillable_contracts_at_limit for obs in touches) / len(touches)
    return FillValidationSummary(
        token_id=first.token_id,
        side=first.side,
        limit_price=first.limit_price,
        contracts=first.contracts,
        observations=len(observations),
        replay_touches=len(touches),
        full_fill_observations=len(full),
        partial_fill_observations=len(partial),
        min_order_size_blocks=len(min_blocks),
        touch_rate=len(touches) / len(observations),
        full_fill_rate=len(full) / len(observations),
        partial_fill_rate=len(partial) / len(observations),
        replay_overstates_full_fill_count=sum(1 for obs in observations if obs.replay_touch and not obs.fully_fillable_at_limit),
        avg_fillable_contracts_when_touched=avg_fillable,
    )


def run_observation_loop(
    *,
    client: PolymarketPublicClient,
    token_id: str,
    side: str,
    limit_price: float,
    contracts: float,
    samples: int,
    interval_seconds: float,
) -> tuple[list[FillValidationObservation], FillValidationSummary]:
    observations: list[FillValidationObservation] = []
    for index in range(samples):
        book = client.get_order_book(token_id)
        observations.append(observe_fillability(book=book, side=side, limit_price=limit_price, contracts=contracts))
        if index < samples - 1 and interval_seconds > 0:
            time.sleep(interval_seconds)
    return observations, summarize_observations(observations)


def write_observations_sqlite(path: Path, observations: list[FillValidationObservation], summary: FillValidationSummary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS polymarket_fill_observations (
                ts TEXT NOT NULL,
                token_id TEXT NOT NULL,
                side TEXT NOT NULL,
                limit_price REAL NOT NULL,
                contracts REAL NOT NULL,
                best_bid REAL,
                best_ask REAL,
                spread REAL,
                min_order_size REAL,
                tick_size REAL,
                replay_touch INTEGER NOT NULL,
                fillable_contracts_at_limit REAL NOT NULL,
                fillable_notional_at_limit REAL NOT NULL,
                fully_fillable_at_limit INTEGER NOT NULL,
                partial_fill_expected INTEGER NOT NULL,
                top_level_size REAL,
                book_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS polymarket_fill_summary (
                created_at TEXT NOT NULL,
                token_id TEXT NOT NULL,
                side TEXT NOT NULL,
                limit_price REAL NOT NULL,
                contracts REAL NOT NULL,
                observations INTEGER NOT NULL,
                replay_touches INTEGER NOT NULL,
                full_fill_observations INTEGER NOT NULL,
                partial_fill_observations INTEGER NOT NULL,
                min_order_size_blocks INTEGER NOT NULL,
                touch_rate REAL NOT NULL,
                full_fill_rate REAL NOT NULL,
                partial_fill_rate REAL NOT NULL,
                replay_overstates_full_fill_count INTEGER NOT NULL,
                avg_fillable_contracts_when_touched REAL
            );
            """
        )
        conn.executemany(
            """
            INSERT INTO polymarket_fill_observations (
                ts, token_id, side, limit_price, contracts, best_bid, best_ask, spread,
                min_order_size, tick_size, replay_touch, fillable_contracts_at_limit,
                fillable_notional_at_limit, fully_fillable_at_limit, partial_fill_expected,
                top_level_size, book_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    obs.ts,
                    obs.token_id,
                    obs.side,
                    obs.limit_price,
                    obs.contracts,
                    obs.best_bid,
                    obs.best_ask,
                    obs.spread,
                    obs.min_order_size,
                    obs.tick_size,
                    1 if obs.replay_touch else 0,
                    obs.fillable_contracts_at_limit,
                    obs.fillable_notional_at_limit,
                    1 if obs.fully_fillable_at_limit else 0,
                    1 if obs.partial_fill_expected else 0,
                    obs.top_level_size,
                    obs.book_hash,
                )
                for obs in observations
            ],
        )
        conn.execute(
            """
            INSERT INTO polymarket_fill_summary (
                created_at, token_id, side, limit_price, contracts, observations,
                replay_touches, full_fill_observations, partial_fill_observations,
                min_order_size_blocks, touch_rate, full_fill_rate, partial_fill_rate,
                replay_overstates_full_fill_count, avg_fillable_contracts_when_touched
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(tz=UTC).isoformat(),
                summary.token_id,
                summary.side,
                summary.limit_price,
                summary.contracts,
                summary.observations,
                summary.replay_touches,
                summary.full_fill_observations,
                summary.partial_fill_observations,
                summary.min_order_size_blocks,
                summary.touch_rate,
                summary.full_fill_rate,
                summary.partial_fill_rate,
                summary.replay_overstates_full_fill_count,
                summary.avg_fillable_contracts_when_touched,
            ),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kbtc-poly-fill-validate",
        description="Polymarket public-orderbook fill validation. Defaults to observe/dry-run; no orders are placed.",
    )
    parser.add_argument("--token-id", required=True, help="Polymarket CLOB token/asset ID to observe.")
    parser.add_argument("--side", choices=["BUY", "SELL"], default="BUY")
    parser.add_argument("--limit-price", type=float, required=True, help="Limit price to test against current CLOB depth.")
    parser.add_argument("--contracts", type=float, default=5.0, help="Contracts/shares to test. Polymarket min is usually 5.")
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--clob-url", default=POLYMARKET_CLOB_URL)
    parser.add_argument("--sqlite-out", default=None, help="Optional SQLite file to append observations/summary.")
    parser.add_argument("--json", action="store_true", help="Print JSON payload instead of compact text.")
    parser.add_argument("--place-order", action="store_true", help="Reserved for future live-paper validation. Currently refused.")
    parser.add_argument("--allow-live-orders", action="store_true", help="Required with --place-order in a future implementation; currently still refused.")
    parser.add_argument("--confirm-polymarket-live-orders", default="", help="Must equal POLYMARKET_LIVE_ORDER_TEST in a future implementation; currently still refused.")
    args = parser.parse_args(argv)

    if args.place_order:
        print(
            "refusing to place Polymarket orders: authenticated place/cancel is intentionally not implemented yet; run without --place-order for public orderbook validation",
            file=sys.stderr,
        )
        return 2
    if args.samples <= 0:
        print("--samples must be positive", file=sys.stderr)
        return 2

    client = PolymarketPublicClient(base_url=args.clob_url)
    observations, summary = run_observation_loop(
        client=client,
        token_id=args.token_id,
        side=args.side,
        limit_price=args.limit_price,
        contracts=args.contracts,
        samples=args.samples,
        interval_seconds=args.interval_seconds,
    )
    if args.sqlite_out:
        write_observations_sqlite(Path(args.sqlite_out), observations, summary)
    payload = {"summary": asdict(summary), "observations": [asdict(obs) for obs in observations]}
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(
            "polymarket_fill_validation "
            f"token_id={summary.token_id} side={summary.side} limit={summary.limit_price} "
            f"contracts={summary.contracts} observations={summary.observations} "
            f"touch_rate={summary.touch_rate:.3f} full_fill_rate={summary.full_fill_rate:.3f} "
            f"partial_fill_rate={summary.partial_fill_rate:.3f} "
            f"replay_overstates={summary.replay_overstates_full_fill_count}"
        )
    return 0


def _levels(value: Any, *, reverse: bool) -> tuple[PolymarketBookLevel, ...]:
    if not isinstance(value, list):
        return ()
    levels = []
    for item in value:
        if not isinstance(item, dict):
            continue
        price = _float_or_none(item.get("price"))
        size = _float_or_none(item.get("size"))
        if price is None or size is None or price <= 0 or size <= 0:
            continue
        levels.append(PolymarketBookLevel(price=price, size=size))
    return tuple(sorted(levels, key=lambda level: level.price, reverse=reverse))


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
