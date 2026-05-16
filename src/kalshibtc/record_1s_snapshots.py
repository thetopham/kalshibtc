from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from .datafeed.models import Tick
from .runtime_paths import PREFERRED_SNAPSHOT_DB, resolve_snapshot_db
from .strategy.slope import SlopeTracker

KALSHI_PUBLIC_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
COINBASE_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
STREAM_TABLE = "realtime_snapshots_1s"
DEFAULT_SERIES_TICKER = "KXBTC15M"

SNAPSHOT_COLUMNS = [
    "ts",
    "market_ticker",
    "market_open_time",
    "market_close_time",
    "btc_price",
    "strike",
    "target_price",
    "distance_from_strike",
    "seconds_to_close",
    "btc_velocity_30s",
    "slope_30s",
    "yes_bid",
    "yes_ask",
    "no_bid",
    "no_ask",
    "orderbook_sequence",
    "execution_blocked_by_json",
    "raw_state_json",
    "raw_json",
    "created_at",
]

_OPTIONAL_COLUMN_DEFS = {
    "market_open_time": "TEXT",
    "market_close_time": "TEXT",
    "btc_price": "REAL",
    "strike": "REAL",
    "target_price": "REAL",
    "distance_from_strike": "REAL",
    "seconds_to_close": "REAL",
    "btc_velocity_30s": "REAL",
    "slope_30s": "REAL",
    "yes_bid": "REAL",
    "yes_ask": "REAL",
    "no_bid": "REAL",
    "no_ask": "REAL",
    "orderbook_sequence": "INTEGER",
    "execution_blocked_by_json": "TEXT",
    "raw_state_json": "TEXT",
    "raw_json": "TEXT",
    "created_at": "TEXT",
}

__all__ = [
    "COINBASE_SPOT_URL",
    "DEFAULT_SERIES_TICKER",
    "KALSHI_PUBLIC_BASE_URL",
    "RealtimeSnapshotRecorder",
    "SNAPSHOT_COLUMNS",
    "STREAM_TABLE",
    "PublicRestSnapshotSource",
    "main",
    "snapshot_row_from_payload",
]


class RealtimeSnapshotRecorder:
    """SQLite writer for paper-executor-compatible 1s snapshots.

    This class owns only the recorder DB. It has no strategy, paper execution, or
    order-submission behavior.
    """

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
        update_columns = [column for column in SNAPSHOT_COLUMNS if column not in {"market_ticker", "ts"}]
        updates = ", ".join(f"{column}=excluded.{column}" for column in update_columns)
        values = tuple(row[column] for column in SNAPSHOT_COLUMNS)
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                INSERT INTO {STREAM_TABLE} ({columns}) VALUES ({placeholders})
                ON CONFLICT(market_ticker, ts) DO UPDATE SET {updates}
                """,
                values,
            )
        return cursor.rowcount == 1

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                f"""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS {STREAM_TABLE} (
                    ts TEXT NOT NULL,
                    market_ticker TEXT NOT NULL,
                    market_open_time TEXT,
                    market_close_time TEXT NOT NULL,
                    btc_price REAL NOT NULL,
                    strike REAL NOT NULL,
                    target_price REAL,
                    distance_from_strike REAL,
                    seconds_to_close REAL,
                    btc_velocity_30s REAL,
                    slope_30s REAL,
                    yes_bid REAL,
                    yes_ask REAL,
                    no_bid REAL,
                    no_ask REAL,
                    orderbook_sequence INTEGER,
                    execution_blocked_by_json TEXT NOT NULL,
                    raw_state_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (market_ticker, ts)
                );
                """
            )
            self._ensure_columns(conn)
            conn.executescript(
                f"""
                CREATE INDEX IF NOT EXISTS idx_realtime_snapshots_1s_ts
                    ON {STREAM_TABLE}(ts);
                CREATE INDEX IF NOT EXISTS idx_realtime_snapshots_1s_market_close
                    ON {STREAM_TABLE}(market_ticker, seconds_to_close);
                """
            )

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection) -> None:
        columns = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({STREAM_TABLE})")}
        for column, definition in _OPTIONAL_COLUMN_DEFS.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE {STREAM_TABLE} ADD COLUMN {column} {definition}")


class PublicRestSnapshotSource:
    """Read-only public-data snapshot source for the active recorder CLI.

    It polls Coinbase spot BTC and Kalshi public market/orderbook endpoints. It
    intentionally has no credentials and no order methods.
    """

    def __init__(
        self,
        *,
        kalshi_base_url: str = KALSHI_PUBLIC_BASE_URL,
        coinbase_spot_url: str = COINBASE_SPOT_URL,
        series_ticker: str = DEFAULT_SERIES_TICKER,
        market_ticker: str | None = None,
        timeout_seconds: int = 10,
        session: Any | None = None,
    ) -> None:
        self.kalshi_base_url = kalshi_base_url.rstrip("/")
        self.coinbase_spot_url = coinbase_spot_url
        self.series_ticker = series_ticker
        self.market_ticker = market_ticker
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "kalshibtc-1s-recorder/0.1"})
        self.slope_tracker = SlopeTracker(window_seconds=30.0)
        self._cached_market_ticker: str | None = market_ticker
        self._cached_close_time: datetime | None = None

    def snapshot(self) -> dict[str, Any]:
        now = _now_utc()
        btc_price = self._btc_price()
        market = self._current_market(now=now)
        quote = self._top_orderbook_quote(str(market["ticker"]))
        tick = Tick(ts=now, price=btc_price, source="coinbase_rest", raw={"url": self.coinbase_spot_url})
        self.slope_tracker.add(tick)
        slope_30s = self.slope_tracker.velocity()

        close_time = _parse_datetime(market.get("close_time")) or _parse_datetime(
            market.get("expected_expiration_time")
        )
        if close_time is None:
            raise ValueError(f"Kalshi market {market['ticker']} has no close_time")
        open_time = _parse_datetime(market.get("open_time"))
        strike = _market_strike(market)
        if strike is None:
            raise ValueError(f"Kalshi market {market['ticker']} has no strike/floor_strike")

        payload: dict[str, Any] = {
            "ts": now,
            "market_ticker": market["ticker"],
            "market_open_time": open_time,
            "market_close_time": close_time,
            "btc_price": btc_price,
            "strike": strike,
            "target_price": strike,
            "btc_velocity_30s": slope_30s,
            "slope_30s": slope_30s,
            "yes_bid": quote.get("yes_bid") if quote.get("yes_bid") is not None else _prob(market, "yes_bid"),
            "yes_ask": quote.get("yes_ask") if quote.get("yes_ask") is not None else _prob(market, "yes_ask"),
            "no_bid": quote.get("no_bid") if quote.get("no_bid") is not None else _prob(market, "no_bid"),
            "no_ask": quote.get("no_ask") if quote.get("no_ask") is not None else _prob(market, "no_ask"),
            "orderbook_sequence": quote.get("orderbook_sequence"),
            "source": "public_rest",
            "market_raw": market,
            "orderbook_raw": quote.get("raw"),
        }
        return payload

    def _btc_price(self) -> float:
        response = self.session.get(self.coinbase_spot_url, timeout=self.timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, Mapping):
            data = payload.get("data")
            if isinstance(data, Mapping):
                price = _float_or_none(data.get("amount"))
                if price is not None:
                    return price
            price = _float_or_none(payload.get("price"))
            if price is not None:
                return price
        raise ValueError("Coinbase spot response did not include a BTC price")

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.session.get(
            f"{self.kalshi_base_url}{path}", params=params, timeout=self.timeout_seconds
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def _current_market(self, *, now: datetime) -> dict[str, Any]:
        if self.market_ticker:
            return self._get_market(self.market_ticker)
        if (
            self._cached_market_ticker
            and self._cached_close_time is not None
            and now < self._cached_close_time
        ):
            return self._get_market(self._cached_market_ticker)
        markets = self._list_markets(status="open") or self._list_markets(status=None)
        if not markets:
            raise LookupError(f"No Kalshi markets found for series {self.series_ticker}")
        live = [market for market in markets if _is_live_market(market, now)]
        timed = [market for market in markets if _parse_datetime(market.get("close_time")) is not None]
        if live:
            market = min(live, key=lambda item: _parse_datetime(item.get("close_time")) or now)
        elif timed:
            upcoming = [item for item in timed if (_parse_datetime(item.get("close_time")) or now) > now]
            market = min(upcoming or timed, key=lambda item: abs((_parse_datetime(item.get("close_time")) or now) - now))
        else:
            market = markets[0]
        self._cached_market_ticker = str(market.get("ticker") or "") or None
        self._cached_close_time = _parse_datetime(market.get("close_time"))
        if self._cached_market_ticker:
            return self._get_market(self._cached_market_ticker)
        return market

    def _list_markets(self, *, status: str | None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"series_ticker": self.series_ticker, "limit": 50}
        if status:
            params["status"] = status
        payload = self._get("/markets", params=params)
        markets = payload.get("markets")
        return [market for market in markets if isinstance(market, dict)] if isinstance(markets, list) else []

    def _get_market(self, ticker: str) -> dict[str, Any]:
        payload = self._get(f"/markets/{ticker}")
        market = payload.get("market", payload)
        if not isinstance(market, dict):
            raise LookupError(f"Kalshi market not found: {ticker}")
        return market

    def _top_orderbook_quote(self, ticker: str) -> dict[str, Any]:
        try:
            payload = self._get(f"/markets/{ticker}/orderbook", params={"depth": 20})
        except requests.RequestException:
            return {"raw": None}
        orderbook = payload.get("orderbook_fp") or payload.get("orderbook") or {}
        if not isinstance(orderbook, Mapping):
            return {"raw": payload}
        yes_bid = _best_bid(orderbook.get("yes_dollars"))
        if yes_bid is None:
            yes_bid = _best_bid(orderbook.get("yes"), cents=True)
        no_bid = _best_bid(orderbook.get("no_dollars"))
        if no_bid is None:
            no_bid = _best_bid(orderbook.get("no"), cents=True)
        return {
            "yes_bid": yes_bid,
            "no_bid": no_bid,
            "yes_ask": round(1.0 - no_bid, 4) if no_bid is not None else None,
            "no_ask": round(1.0 - yes_bid, 4) if yes_bid is not None else None,
            "orderbook_sequence": _int_or_none(
                payload.get("seq") or payload.get("sequence") or orderbook.get("sequence")
            ),
            "raw": payload,
        }


def snapshot_row_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_ts = _parse_datetime(payload.get("ts") or payload.get("as_of") or payload.get("btc_ts"))
    ts = raw_ts or _now_utc()
    ts_second = ts.astimezone(UTC).replace(microsecond=0).isoformat()
    market_ticker = _required_text(payload, "market_ticker")
    close_time = _parse_datetime(payload.get("market_close_time"))
    if close_time is None:
        raise ValueError("market_close_time is required")
    open_time = _parse_datetime(payload.get("market_open_time"))
    btc_price = _required_float(payload, "btc_price", "current_price", "price")
    strike = _required_float(payload, "strike", "target_price", "floor_strike")
    target_price = _float_or_none(payload.get("target_price")) or strike
    distance = _float_or_none(payload.get("distance_from_strike"))
    if distance is None:
        distance = btc_price - target_price
    seconds_to_close = _float_or_none(payload.get("seconds_to_close"))
    if seconds_to_close is None:
        seconds_to_close = (close_time - ts.astimezone(UTC)).total_seconds()
    slope = _first_float(payload, "btc_velocity_30s", "slope_30s")
    raw_json = _json(payload)
    return {
        "ts": ts_second,
        "market_ticker": market_ticker,
        "market_open_time": open_time.astimezone(UTC).isoformat() if open_time else None,
        "market_close_time": close_time.astimezone(UTC).isoformat(),
        "btc_price": round(btc_price, 2),
        "strike": round(strike, 2),
        "target_price": round(target_price, 2),
        "distance_from_strike": round(distance, 2),
        "seconds_to_close": round(seconds_to_close, 3),
        "btc_velocity_30s": _round_or_none(slope, 6),
        "slope_30s": _round_or_none(slope, 6),
        "yes_bid": _round_or_none(_float_or_none(payload.get("yes_bid")), 4),
        "yes_ask": _round_or_none(_float_or_none(payload.get("yes_ask")), 4),
        "no_bid": _round_or_none(_float_or_none(payload.get("no_bid")), 4),
        "no_ask": _round_or_none(_float_or_none(payload.get("no_ask")), 4),
        "orderbook_sequence": _int_or_none(payload.get("orderbook_sequence")),
        "execution_blocked_by_json": _json(payload.get("execution_blocked_by", [])),
        "raw_state_json": raw_json,
        "raw_json": raw_json,
        "created_at": _now_utc().replace(microsecond=0).isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m kalshibtc.record_1s_snapshots",
        description="Record read-only 1s Kalshi BTC snapshots into the stream DB.",
    )
    parser.add_argument(
        "--snapshot-db",
        default=None,
        help=(
            "Recorder-owned SQLite DB that receives realtime_snapshots_1s rows. "
            f"Defaults to {PREFERRED_SNAPSHOT_DB}, falling back to the old data/ path if already present."
        ),
    )
    parser.add_argument(
        "--emit-min-interval-seconds",
        type=float,
        default=1.0,
        help="Minimum seconds between recorded snapshots when --loop is set.",
    )
    parser.add_argument("--loop", action="store_true", help="Continuously record snapshots until stopped.")
    parser.add_argument(
        "--max-events",
        type=int,
        default=None,
        help="Stop after N successful snapshots; useful for smoke tests.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON status lines.")
    parser.add_argument("--series-ticker", default=DEFAULT_SERIES_TICKER)
    parser.add_argument(
        "--market-ticker",
        default=None,
        help="Optional fixed Kalshi market ticker; otherwise discovers the current KXBTC15M market.",
    )
    parser.add_argument("--kalshi-base-url", default=KALSHI_PUBLIC_BASE_URL)
    parser.add_argument("--coinbase-spot-url", default=COINBASE_SPOT_URL)
    parser.add_argument("--timeout-seconds", type=int, default=10)
    parser.add_argument(
        "--max-consecutive-errors",
        type=int,
        default=5,
        help="Exit non-zero after this many consecutive public-data errors in loop mode.",
    )
    args = parser.parse_args(argv)
    snapshot_db = resolve_snapshot_db(args.snapshot_db)

    recorder = RealtimeSnapshotRecorder(snapshot_db)
    source = PublicRestSnapshotSource(
        kalshi_base_url=args.kalshi_base_url,
        coinbase_spot_url=args.coinbase_spot_url,
        series_ticker=args.series_ticker,
        market_ticker=args.market_ticker,
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
                    "event": "record_1s_error",
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
            _emit(
                {
                    "event": "record_1s_snapshot",
                    "snapshot_db": str(snapshot_db),
                    "market_ticker": payload["market_ticker"],
                    "ts": snapshot_row_from_payload(payload)["ts"],
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
    if payload.get("event") == "record_1s_snapshot":
        print(
            "1s_recorder "
            f"market={payload.get('market_ticker')} "
            f"ts={payload.get('ts')} "
            f"recorded={payload.get('recorded')}",
            flush=True,
        )
        return
    print(f"1s_recorder_error: {payload.get('error')}", flush=True)


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


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


def _first_float(payload: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in payload:
            value = _float_or_none(payload.get(key))
            if value is not None:
                return value
    return None


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return parsed


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _round_or_none(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


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


def _prob(mapping: Mapping[str, Any], key: str) -> float | None:
    dollars_value = _float_or_none(mapping.get(f"{key}_dollars"))
    if dollars_value is not None:
        return round(dollars_value, 4)
    cents_or_dollars = _float_or_none(mapping.get(key))
    if cents_or_dollars is None:
        return None
    if cents_or_dollars >= 1.0:
        return round(cents_or_dollars / 100.0, 4)
    return round(cents_or_dollars, 4)


def _market_strike(market: Mapping[str, Any]) -> float | None:
    value = _first_float(market, "floor_strike", "strike", "target_price")
    if value is not None:
        return value
    for key in ("subtitle", "title", "rules_primary"):
        text = market.get(key)
        if isinstance(text, str):
            match = re.search(r"\$?([0-9][0-9,]*(?:\.\d+)?)", text)
            if match:
                return float(match.group(1).replace(",", ""))
    return None


def _is_live_market(market: Mapping[str, Any], now: datetime) -> bool:
    open_time = _parse_datetime(market.get("open_time"))
    close_time = _parse_datetime(market.get("close_time"))
    return bool(open_time is not None and close_time is not None and open_time <= now < close_time)


def _best_bid(levels: Any, *, cents: bool = False) -> float | None:
    best: float | None = None
    if not isinstance(levels, list):
        return None
    for level in levels:
        if not isinstance(level, list | tuple) or not level:
            continue
        price = _float_or_none(level[0])
        if price is None:
            continue
        if cents:
            price = price / 100.0
        if 0.0 < price <= 1.0 and (best is None or price > best):
            best = price
    return round(best, 4) if best is not None else None


if __name__ == "__main__":
    raise SystemExit(main())
