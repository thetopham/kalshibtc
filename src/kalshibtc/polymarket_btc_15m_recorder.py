from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from .polymarket_chainlink import (
    CHAINLINK_RTDS_URL,
    ChainlinkPriceStore,
    ChainlinkRTDSBackgroundClient,
)
from .record_1s_snapshots import COINBASE_SPOT_URL, PRICE_SOURCE_COINBASE, _float_or_none
from .strategy.slope import SlopeTracker
from .datafeed.models import Tick

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"
DEFAULT_EVENT_SLUG = "btc-updown-15m-1779089400"
DEFAULT_DB_PATH = "feed/polymarket-btc-1s.sqlite3"
BINANCE_US_BTC_URL = "https://api.binance.us/api/v3/ticker/price?symbol=BTCUSDT"
PRICE_SOURCE_BINANCE_US = "binance_us"
PRICE_SOURCE_CHAINLINK_RTDS = "chainlink_rtds"
STREAM_TABLE = "realtime_snapshots_1s"

SNAPSHOT_COLUMNS = [
    "ts",
    "market_slug",
    "condition_id",
    "yes_token_id",
    "no_token_id",
    "market_open_time",
    "market_close_time",
    "seconds_to_close",
    "btc_price",
    "strike",
    "target_price",
    "yes_bid",
    "yes_ask",
    "no_bid",
    "no_ask",
    "yes_orderbook_json",
    "no_orderbook_json",
    "raw_market_json",
    "raw_book_json",
    # Compatibility columns for existing feed/replay/dashboard readers.
    "market_ticker",
    "distance_from_strike",
    "btc_velocity_30s",
    "slope_30s",
    "orderbook_sequence",
    "yes_spread",
    "no_spread",
    "yes_top_bid_size",
    "yes_top_ask_size",
    "no_top_bid_size",
    "no_top_ask_size",
    "yes_bid_depth_5_ticks",
    "yes_ask_depth_5_ticks",
    "no_bid_depth_5_ticks",
    "no_ask_depth_5_ticks",
    "orderbook_imbalance",
    "liquidity",
    "volume",
    "volume_24h",
    "execution_blocked_by_json",
    "raw_state_json",
    "raw_json",
    "created_at",
]

_COLUMN_DEFS = {
    "ts": "TEXT",
    "market_slug": "TEXT",
    "condition_id": "TEXT",
    "yes_token_id": "TEXT",
    "no_token_id": "TEXT",
    "market_open_time": "TEXT",
    "market_close_time": "TEXT",
    "seconds_to_close": "REAL",
    "btc_price": "REAL",
    "strike": "REAL",
    "target_price": "REAL",
    "yes_bid": "REAL",
    "yes_ask": "REAL",
    "no_bid": "REAL",
    "no_ask": "REAL",
    "yes_orderbook_json": "TEXT",
    "no_orderbook_json": "TEXT",
    "raw_market_json": "TEXT",
    "raw_book_json": "TEXT",
    "market_ticker": "TEXT",
    "distance_from_strike": "REAL",
    "btc_velocity_30s": "REAL",
    "slope_30s": "REAL",
    "orderbook_sequence": "INTEGER",
    "yes_spread": "REAL",
    "no_spread": "REAL",
    "yes_top_bid_size": "REAL",
    "yes_top_ask_size": "REAL",
    "no_top_bid_size": "REAL",
    "no_top_ask_size": "REAL",
    "yes_bid_depth_5_ticks": "REAL",
    "yes_ask_depth_5_ticks": "REAL",
    "no_bid_depth_5_ticks": "REAL",
    "no_ask_depth_5_ticks": "REAL",
    "orderbook_imbalance": "REAL",
    "liquidity": "REAL",
    "volume": "REAL",
    "volume_24h": "REAL",
    "execution_blocked_by_json": "TEXT",
    "raw_state_json": "TEXT",
    "raw_json": "TEXT",
    "created_at": "TEXT",
}


class PolymarketBTC15mRecorder:
    """SQLite writer for read-only Polymarket BTC up/down snapshots."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def record_snapshot(self, payload: Mapping[str, Any]) -> bool:
        row = snapshot_row_from_payload(payload)
        columns = ", ".join(SNAPSHOT_COLUMNS)
        placeholders = ", ".join("?" for _ in SNAPSHOT_COLUMNS)
        updates = ", ".join(
            f"{column}=excluded.{column}" for column in SNAPSHOT_COLUMNS if column not in {"market_slug", "ts"}
        )
        values = tuple(row[column] for column in SNAPSHOT_COLUMNS)
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                INSERT INTO {STREAM_TABLE} ({columns}) VALUES ({placeholders})
                ON CONFLICT(market_slug, ts) DO UPDATE SET {updates}
                """,
                values,
            )
        return cursor.rowcount == 1

    def _init_db(self) -> None:
        defs = ",\n                    ".join(f"{name} {definition}" for name, definition in _COLUMN_DEFS.items())
        with self.connect() as conn:
            conn.executescript(
                f"""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS {STREAM_TABLE} (
                    {defs},
                    PRIMARY KEY (market_slug, ts)
                );
                """
            )
            self._ensure_columns(conn)
            conn.executescript(
                f"""
                CREATE INDEX IF NOT EXISTS idx_poly_realtime_snapshots_1s_ts
                    ON {STREAM_TABLE}(ts);
                CREATE INDEX IF NOT EXISTS idx_poly_realtime_snapshots_1s_market_close
                    ON {STREAM_TABLE}(market_slug, seconds_to_close);
                CREATE INDEX IF NOT EXISTS idx_realtime_snapshots_1s_ts
                    ON {STREAM_TABLE}(ts);
                CREATE INDEX IF NOT EXISTS idx_realtime_snapshots_1s_market_close
                    ON {STREAM_TABLE}(market_ticker, seconds_to_close);
                """
            )

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection) -> None:
        columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({STREAM_TABLE})")}
        for column, definition in _COLUMN_DEFS.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE {STREAM_TABLE} ADD COLUMN {column} {definition}")


class PolymarketBTC15mSnapshotSource:
    """Read-only public Polymarket Gamma/CLOB snapshot source.

    It has no wallet, credentials, signing, or order-submission behavior.
    """

    def __init__(
        self,
        *,
        event_slug: str = DEFAULT_EVENT_SLUG,
        auto_advance_slug: bool = True,
        gamma_base_url: str = GAMMA_BASE_URL,
        clob_base_url: str = CLOB_BASE_URL,
        coinbase_spot_url: str = COINBASE_SPOT_URL,
        btc_price_source: str = PRICE_SOURCE_COINBASE,
        binance_us_btc_url: str = BINANCE_US_BTC_URL,
        chainlink_store: ChainlinkPriceStore | None = None,
        timeout_seconds: int = 10,
        session: Any | None = None,
    ) -> None:
        self.event_slug = event_slug
        self.auto_advance_slug = auto_advance_slug
        self.gamma_base_url = gamma_base_url.rstrip("/")
        self.clob_base_url = clob_base_url.rstrip("/")
        self.coinbase_spot_url = coinbase_spot_url
        self.btc_price_source = btc_price_source
        self.binance_us_btc_url = binance_us_btc_url
        self.chainlink_store = chainlink_store
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "polymarket-btc-15m-recorder/0.1"})
        self.slope_tracker = SlopeTracker(window_seconds=30.0)
        self._metadata_cache: dict[str, dict[str, Any]] = {}

    def snapshot(self) -> dict[str, Any]:
        now = _now_utc()
        event = self._current_event(now=now)
        market = _select_binary_market(event)
        token_ids = _clob_token_ids(market)
        if len(token_ids) < 2:
            raise ValueError(f"Polymarket market {market.get('slug') or self.event_slug} missing two clobTokenIds")
        outcomes = _outcomes(market)
        yes_index = _outcome_index(outcomes, "yes", default=0)
        no_index = _outcome_index(outcomes, "no", default=1)
        yes_token = token_ids[yes_index]
        no_token = token_ids[no_index]
        yes_book = self._book(yes_token)
        no_book = self._book(no_token)
        btc_price, btc_raw = self._btc_price()
        tick = Tick(ts=now, price=btc_price, source=str(btc_raw.get("source") or self.btc_price_source), raw=btc_raw)
        self.slope_tracker.add(tick)
        slope_30s = self.slope_tracker.velocity()
        open_time = _polymarket_market_open_time(market, event)
        close_time = _first_datetime(market, event, "endDate", "endDateIso", "endDateIso", "close_time")
        if close_time is None:
            raise ValueError(f"Polymarket market {market.get('slug') or self.event_slug} missing endDate")
        market_slug = str(market.get("slug") or event.get("slug") or self.event_slug)
        strike = _market_strike(market, event)
        reference_raw = None
        if self.chainlink_store is not None:
            reference_tick = self.chainlink_store.reference_for_market(market_slug=market_slug, open_time=open_time)
            if reference_tick is not None:
                strike = reference_tick.price
                reference_raw = {
                    "source": "polymarket_chainlink_rtds_open_reference",
                    "ts": reference_tick.ts.isoformat(),
                    "price": reference_tick.price,
                    "raw": reference_tick.raw,
                }
        metadata = self._event_metadata_from_page(market_slug)
        metadata_price_to_beat = _metadata_price_to_beat(metadata)
        if metadata_price_to_beat is not None and reference_raw is None:
            strike = metadata_price_to_beat
            reference_raw = {"source": "polymarket_web_event_metadata", "eventMetadata": metadata}
        condition_id = str(market.get("conditionId") or market.get("condition_id") or yes_book.get("market") or "")
        raw_book = {"yes": yes_book, "no": no_book}
        return {
            "ts": now,
            "market_slug": market_slug,
            "condition_id": condition_id,
            "yes_token_id": str(yes_token),
            "no_token_id": str(no_token),
            "market_open_time": open_time,
            "market_close_time": close_time,
            "seconds_to_close": (close_time - now).total_seconds(),
            "btc_price": btc_price,
            "strike": strike,
            "target_price": strike,
            "yes_bid": _best_bid(yes_book.get("bids")),
            "yes_ask": _best_ask(yes_book.get("asks")),
            "no_bid": _best_bid(no_book.get("bids")),
            "no_ask": _best_ask(no_book.get("asks")),
            "yes_orderbook": yes_book,
            "no_orderbook": no_book,
            "raw_market": market,
            "raw_event": event,
            "raw_book": raw_book,
            "btc_price_raw": btc_raw,
            "reference_price_raw": reference_raw,
            "btc_velocity_30s": slope_30s,
            "slope_30s": slope_30s,
        }

    def _event_by_slug(self, slug: str) -> dict[str, Any]:
        response = self.session.get(f"{self.gamma_base_url}/events/slug/{slug}", timeout=self.timeout_seconds)
        if response.status_code == 404:
            response = self.session.get(
                f"{self.gamma_base_url}/events", params={"slug": slug}, timeout=self.timeout_seconds
            )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            if not payload:
                raise LookupError(f"No Polymarket event found for slug {slug}")
            payload = payload[0]
        if not isinstance(payload, Mapping):
            raise ValueError("Polymarket event response was not a JSON object")
        return dict(payload)

    def _current_event(self, *, now: datetime) -> dict[str, Any]:
        slug = self.event_slug
        candidates = [slug]
        if self.auto_advance_slug:
            candidates.extend(_nearby_updown_slugs(slug=slug, now=now))
        last_event: dict[str, Any] | None = None
        for candidate in dict.fromkeys(candidates):
            try:
                event = self._event_by_slug(candidate)
            except (requests.RequestException, LookupError, ValueError):
                continue
            last_event = event
            market = _select_binary_market(event)
            close = _first_datetime(market, event, "endDate", "endDateIso", "close_time")
            if close is None or close >= now:
                self.event_slug = candidate
                return event
        if last_event is not None:
            return last_event
        return self._event_by_slug(slug)

    def _book(self, token_id: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.clob_base_url}/book", params={"token_id": token_id}, timeout=self.timeout_seconds
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("Polymarket CLOB book response was not a JSON object")
        return dict(payload)

    def _btc_price(self) -> tuple[float, dict[str, Any]]:
        if self.btc_price_source == PRICE_SOURCE_CHAINLINK_RTDS:
            tick = self.chainlink_store.latest_tick(max_age_seconds=30.0) if self.chainlink_store else None
            if tick is not None:
                return tick.price, {
                    "source": PRICE_SOURCE_CHAINLINK_RTDS,
                    "url": CHAINLINK_RTDS_URL,
                    "ts": tick.ts.isoformat(),
                    "payload": tick.raw,
                }
            raise ValueError("No fresh Chainlink RTDS BTC/USD tick available")
        if self.btc_price_source == PRICE_SOURCE_BINANCE_US:
            return self._binance_us_btc_price()
        if self.btc_price_source != PRICE_SOURCE_COINBASE:
            raise ValueError(f"Unsupported BTC price source: {self.btc_price_source}")
        response = self.session.get(self.coinbase_spot_url, timeout=self.timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, Mapping):
            data = payload.get("data")
            if isinstance(data, Mapping):
                price = _float_or_none(data.get("amount"))
                if price is not None:
                    return price, {"source": PRICE_SOURCE_COINBASE, "url": self.coinbase_spot_url, "payload": payload}
            price = _float_or_none(payload.get("price"))
            if price is not None:
                return price, {"source": PRICE_SOURCE_COINBASE, "url": self.coinbase_spot_url, "payload": payload}
        raise ValueError("Coinbase spot response did not include a BTC price")

    def _binance_us_btc_price(self) -> tuple[float, dict[str, Any]]:
        response = self.session.get(self.binance_us_btc_url, timeout=self.timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, Mapping):
            price = _float_or_none(payload.get("price"))
            if price is not None:
                return price, {"source": PRICE_SOURCE_BINANCE_US, "url": self.binance_us_btc_url, "payload": payload}
        raise ValueError("Binance.US ticker response did not include a BTC price")

    def _event_metadata_from_page(self, slug: str) -> dict[str, Any] | None:
        if slug in self._metadata_cache:
            return self._metadata_cache[slug]
        try:
            response = self.session.get(f"https://polymarket.com/event/{slug}", timeout=self.timeout_seconds)
            response.raise_for_status()
        except (requests.RequestException, IndexError):
            return None
        text = getattr(response, "text", None)
        if not isinstance(text, str):
            return None
        metadata = _extract_event_metadata_from_html(text, slug)
        if metadata is not None:
            self._metadata_cache[slug] = metadata
        return metadata


def snapshot_row_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    ts = (_parse_datetime(payload.get("ts")) or _now_utc()).astimezone(UTC).replace(microsecond=0)
    close_time = _parse_datetime(payload.get("market_close_time"))
    if close_time is None:
        raise ValueError("market_close_time is required")
    open_time = _parse_datetime(payload.get("market_open_time"))
    market_slug = _required_text(payload, "market_slug")
    condition_id = str(payload.get("condition_id") or "")
    yes_token_id = str(payload.get("yes_token_id") or "")
    no_token_id = str(payload.get("no_token_id") or "")
    btc_price = _required_float(payload, "btc_price")
    strike = _float_or_none(payload.get("strike"))
    target_price = _float_or_none(payload.get("target_price")) or strike
    seconds_to_close = _float_or_none(payload.get("seconds_to_close"))
    if seconds_to_close is None:
        seconds_to_close = (close_time - ts).total_seconds()
    distance = None if target_price is None else btc_price - target_price
    raw_market = payload.get("raw_market") or payload.get("raw_event") or {}
    raw_book = payload.get("raw_book") or {"yes": payload.get("yes_orderbook"), "no": payload.get("no_orderbook")}
    raw_state = {
        "venue": "polymarket",
        "market_slug": market_slug,
        "condition_id": condition_id,
        "yes_token_id": yes_token_id,
        "no_token_id": no_token_id,
        "raw_market": raw_market,
        "raw_book": raw_book,
        "btc_price_raw": payload.get("btc_price_raw"),
        "reference_price_raw": payload.get("reference_price_raw"),
    }
    slope = _first_float(payload, "btc_velocity_30s", "slope_30s")
    try:
        from .feed_derived_columns import derive_snapshot_columns

        derived = derive_snapshot_columns(
            {
                **payload,
                "yes_orderbook_json": _json(payload.get("yes_orderbook") or {}),
                "no_orderbook_json": _json(payload.get("no_orderbook") or {}),
                "raw_market_json": _json(raw_market),
                "raw_book_json": _json(raw_book),
            },
            venue="polymarket",
        )
    except Exception:
        derived = {}
    row = {
        "ts": ts.isoformat(),
        "market_slug": market_slug,
        "condition_id": condition_id,
        "yes_token_id": yes_token_id,
        "no_token_id": no_token_id,
        "market_open_time": open_time.astimezone(UTC).isoformat() if open_time else None,
        "market_close_time": close_time.astimezone(UTC).isoformat(),
        "seconds_to_close": round(seconds_to_close, 3),
        "btc_price": round(btc_price, 2),
        "strike": round(strike, 2) if strike is not None else None,
        "target_price": round(target_price, 2) if target_price is not None else None,
        "yes_bid": _round_or_none(_float_or_none(payload.get("yes_bid")), 4),
        "yes_ask": _round_or_none(_float_or_none(payload.get("yes_ask")), 4),
        "no_bid": _round_or_none(_float_or_none(payload.get("no_bid")), 4),
        "no_ask": _round_or_none(_float_or_none(payload.get("no_ask")), 4),
        "yes_orderbook_json": _json(payload.get("yes_orderbook") or {}),
        "no_orderbook_json": _json(payload.get("no_orderbook") or {}),
        "raw_market_json": _json(raw_market),
        "raw_book_json": _json(raw_book),
        "market_ticker": market_slug,
        "distance_from_strike": round(distance, 2) if distance is not None else None,
        "btc_velocity_30s": _round_or_none(slope, 6),
        "slope_30s": _round_or_none(slope, 6),
        "orderbook_sequence": _book_sequence(raw_book),
        "yes_spread": derived.get("yes_spread"),
        "no_spread": derived.get("no_spread"),
        "yes_top_bid_size": derived.get("yes_top_bid_size"),
        "yes_top_ask_size": derived.get("yes_top_ask_size"),
        "no_top_bid_size": derived.get("no_top_bid_size"),
        "no_top_ask_size": derived.get("no_top_ask_size"),
        "yes_bid_depth_5_ticks": derived.get("yes_bid_depth_5_ticks"),
        "yes_ask_depth_5_ticks": derived.get("yes_ask_depth_5_ticks"),
        "no_bid_depth_5_ticks": derived.get("no_bid_depth_5_ticks"),
        "no_ask_depth_5_ticks": derived.get("no_ask_depth_5_ticks"),
        "orderbook_imbalance": derived.get("orderbook_imbalance"),
        "liquidity": derived.get("liquidity"),
        "volume": derived.get("volume"),
        "volume_24h": derived.get("volume_24h"),
        "execution_blocked_by_json": "[]",
        "raw_state_json": _json(raw_state),
        "raw_json": _json({**raw_state, "snapshot": dict(payload)}),
        "created_at": _now_utc().replace(microsecond=0).isoformat(),
    }
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="polymarket-btc-15m-recorder",
        description="Record read-only Polymarket BTC 15m up/down snapshots into SQLite.",
    )
    parser.add_argument("--snapshot-db", "--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--event-slug", default=DEFAULT_EVENT_SLUG)
    parser.add_argument("--no-auto-advance-slug", action="store_true", help="Do not infer the current 15m slug from the timestamp suffix when the configured slug expires.")
    parser.add_argument("--gamma-base-url", default=GAMMA_BASE_URL)
    parser.add_argument("--clob-base-url", default=CLOB_BASE_URL)
    parser.add_argument("--coinbase-spot-url", default=COINBASE_SPOT_URL)
    parser.add_argument("--binance-us-btc-url", default=BINANCE_US_BTC_URL)
    parser.add_argument(
        "--btc-price-source",
        choices=[PRICE_SOURCE_COINBASE, PRICE_SOURCE_BINANCE_US, PRICE_SOURCE_CHAINLINK_RTDS],
        default=PRICE_SOURCE_CHAINLINK_RTDS,
        help="BTC/USD source for recorded btc_price. chainlink_rtds matches Polymarket crypto market resolution.",
    )
    parser.add_argument("--chainlink-rtds-url", default=CHAINLINK_RTDS_URL)
    parser.add_argument("--emit-min-interval-seconds", type=float, default=1.0)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--max-events", type=int, default=None)
    parser.add_argument("--timeout-seconds", type=int, default=10)
    parser.add_argument("--max-consecutive-errors", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    recorder = PolymarketBTC15mRecorder(args.snapshot_db)
    chainlink_store = ChainlinkPriceStore(args.snapshot_db)
    chainlink_client = None
    if args.btc_price_source == PRICE_SOURCE_CHAINLINK_RTDS:
        chainlink_client = ChainlinkRTDSBackgroundClient(store=chainlink_store, url=args.chainlink_rtds_url)
        chainlink_client.start()
        deadline = time.monotonic() + max(1.0, min(10.0, float(args.timeout_seconds)))
        while chainlink_store.latest_tick(max_age_seconds=30.0) is None and time.monotonic() < deadline:
            time.sleep(0.1)
    source = PolymarketBTC15mSnapshotSource(
        event_slug=args.event_slug,
        auto_advance_slug=not args.no_auto_advance_slug,
        gamma_base_url=args.gamma_base_url,
        clob_base_url=args.clob_base_url,
        coinbase_spot_url=args.coinbase_spot_url,
        btc_price_source=args.btc_price_source,
        binance_us_btc_url=args.binance_us_btc_url,
        chainlink_store=chainlink_store,
        timeout_seconds=args.timeout_seconds,
    )
    interval = max(0.1, float(args.emit_min_interval_seconds))
    max_consecutive_errors = max(1, int(args.max_consecutive_errors))
    recorded = 0
    consecutive_errors = 0
    while True:
        try:
            payload = source.snapshot()
            recorder.record_snapshot(payload)
        except KeyboardInterrupt:
            raise
        except (requests.RequestException, LookupError, ValueError, sqlite3.Error) as exc:
            consecutive_errors += 1
            _emit(
                {
                    "event": "polymarket_record_1s_error",
                    "error": str(exc),
                    "consecutive_errors": consecutive_errors,
                    "max_consecutive_errors": max_consecutive_errors,
                },
                json_output=args.json,
            )
            if not args.loop or consecutive_errors >= max_consecutive_errors:
                return 1
        else:
            consecutive_errors = 0
            recorded += 1
            row = snapshot_row_from_payload(payload)
            _emit(
                {
                    "event": "polymarket_record_1s_snapshot",
                    "snapshot_db": str(args.snapshot_db),
                    "market_slug": row["market_slug"],
                    "ts": row["ts"],
                    "seconds_to_close": row["seconds_to_close"],
                    "recorded": recorded,
                },
                json_output=args.json,
            )
            if not args.loop or (args.max_events is not None and recorded >= args.max_events):
                return 0
        time.sleep(interval)


def _emit(payload: Mapping[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(_json(payload), flush=True)
        return
    if payload.get("event") == "polymarket_record_1s_snapshot":
        print(
            "polymarket_btc_15m_recorder "
            f"market={payload.get('market_slug')} "
            f"ts={payload.get('ts')} "
            f"seconds_to_close={payload.get('seconds_to_close')} "
            f"recorded={payload.get('recorded')}",
            flush=True,
        )
        return
    print(f"polymarket_btc_15m_recorder_error: {payload.get('error')}", flush=True)


def _nearby_updown_slugs(*, slug: str, now: datetime) -> list[str]:
    match = re.search(r"^(?P<prefix>btc-updown-(?:5m|15m)-)(?P<ts>\d+)$", slug)
    if not match:
        return []
    step = 300 if "-5m-" in slug else 900
    base = int(match.group("ts"))
    current = int(now.timestamp() // step * step)
    candidates: list[int] = []
    for anchor in (current, base):
        for offset in range(-2, 7):
            candidates.append(anchor + offset * step)
    return [f"{match.group('prefix')}{ts}" for ts in candidates if ts > 0]


def _select_binary_market(event: Mapping[str, Any]) -> dict[str, Any]:
    markets = event.get("markets")
    if isinstance(markets, list):
        for market in markets:
            if isinstance(market, Mapping) and len(_clob_token_ids(market)) >= 2:
                return dict(market)
    if len(_clob_token_ids(event)) >= 2:
        return dict(event)
    raise ValueError(f"Polymarket event {event.get('slug')} has no binary CLOB market")


def _clob_token_ids(market: Mapping[str, Any]) -> list[str]:
    value = market.get("clobTokenIds") or market.get("clob_token_ids") or market.get("tokenIds")
    parsed = _parse_jsonish_list(value)
    return [str(item) for item in parsed if item not in (None, "")]


def _outcomes(market: Mapping[str, Any]) -> list[str]:
    return [str(item) for item in _parse_jsonish_list(market.get("outcomes") or market.get("shortOutcomes"))]


def _outcome_index(outcomes: Sequence[str], name: str, *, default: int) -> int:
    for idx, outcome in enumerate(outcomes):
        if outcome.strip().lower() == name.lower():
            return idx
    return default


def _parse_jsonish_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [part.strip() for part in value.split(",") if part.strip()]
        return parsed if isinstance(parsed, list) else []
    return []


def _best_bid(levels: Any) -> float | None:
    prices = [_price_from_level(level) for level in levels] if isinstance(levels, list) else []
    prices = [price for price in prices if price is not None]
    return round(max(prices), 4) if prices else None


def _best_ask(levels: Any) -> float | None:
    prices = [_price_from_level(level) for level in levels] if isinstance(levels, list) else []
    prices = [price for price in prices if price is not None]
    return round(min(prices), 4) if prices else None


def _price_from_level(level: Any) -> float | None:
    if isinstance(level, Mapping):
        return _float_or_none(level.get("price"))
    if isinstance(level, list | tuple) and level:
        return _float_or_none(level[0])
    return None


def _book_sequence(raw_book: Any) -> int | None:
    if not isinstance(raw_book, Mapping):
        return None
    for book in (raw_book.get("yes"), raw_book.get("no")):
        if isinstance(book, Mapping):
            value = book.get("timestamp") or book.get("sequence")
            try:
                return int(float(str(value))) if value not in (None, "") else None
            except (TypeError, ValueError):
                continue
    return None


def _extract_event_metadata_from_html(html: str, slug: str) -> dict[str, Any] | None:
    slug_pos = html.find(f'"slug":"{slug}"')
    if slug_pos < 0:
        return None
    key = '"eventMetadata":'
    meta_pos = html.rfind(key, slug_pos, min(len(html), slug_pos + 12000))
    if meta_pos < 0:
        meta_pos = html.find(key, slug_pos)
    if meta_pos < 0:
        return None
    start = meta_pos + len(key)
    decoder = json.JSONDecoder()
    try:
        value, _ = decoder.raw_decode(html[start:])
    except json.JSONDecodeError:
        return None
    return dict(value) if isinstance(value, Mapping) else None


def _metadata_price_to_beat(metadata: Mapping[str, Any] | None) -> float | None:
    if not metadata:
        return None
    return _float_or_none(metadata.get("priceToBeat"))


def _polymarket_market_open_time(market: Mapping[str, Any], event: Mapping[str, Any]) -> datetime | None:
    slug_time = _timestamp_from_updown_slug(str(market.get("slug") or event.get("slug") or ""))
    if slug_time is not None:
        return slug_time
    return _first_datetime(market, event, "eventStartTime", "startTime", "startDate", "startDateIso", "acceptingOrdersTimestamp", "createdAt")


def _timestamp_from_updown_slug(slug: str) -> datetime | None:
    match = re.search(r"-(\d{10})$", slug)
    if not match:
        return None
    return datetime.fromtimestamp(int(match.group(1)), tz=UTC)


def _market_strike(market: Mapping[str, Any], event: Mapping[str, Any]) -> float | None:
    value = _first_float(market, "strike", "target_price", "target", "xAxisValue")
    if value is not None:
        return value
    for source in (market, event):
        for key in ("question", "title", "description", "slug"):
            text = source.get(key)
            if not isinstance(text, str):
                continue
            # Prefer dollar-denominated BTC targets when present.
            matches = re.findall(r"\$\s*([0-9][0-9,]*(?:\.\d+)?)", text)
            if matches:
                return float(matches[-1].replace(",", ""))
    return None


def _first_datetime(market: Mapping[str, Any], event: Mapping[str, Any], *keys: str) -> datetime | None:
    for mapping in (market, event):
        for key in keys:
            parsed = _parse_datetime(mapping.get(key))
            if parsed is not None:
                return parsed
    return None


def _first_float(payload: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _float_or_none(payload.get(key))
        if value is not None:
            return value
    return None


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if value in (None, ""):
        raise ValueError(f"{key} is required")
    return str(value)


def _required_float(payload: Mapping[str, Any], *keys: str) -> float:
    value = _first_float(payload, *keys)
    if value is None:
        raise ValueError(f"one of {', '.join(keys)} is required")
    return value


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _round_or_none(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


def _json(value: Any) -> str:
    return json.dumps(_jsonable(value), sort_keys=True)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat() if value.tzinfo else value.replace(tzinfo=UTC).isoformat()
    return value


if __name__ == "__main__":
    raise SystemExit(main())
