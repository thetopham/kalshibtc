from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import websockets

CHAINLINK_RTDS_URL = "wss://ws-live-data.polymarket.com"
CHAINLINK_BTC_SYMBOL = "btc/usd"
CHAINLINK_TABLE = "chainlink_btc_usd_ticks"
REFERENCE_TABLE = "polymarket_market_references"


@dataclass(frozen=True)
class ChainlinkPriceTick:
    ts: datetime
    price: float
    raw: dict[str, Any]


class ChainlinkPriceStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                f"""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS {CHAINLINK_TABLE} (
                    ts TEXT PRIMARY KEY,
                    price REAL NOT NULL,
                    raw_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_chainlink_btc_usd_ticks_ts
                    ON {CHAINLINK_TABLE}(ts);
                CREATE TABLE IF NOT EXISTS {REFERENCE_TABLE} (
                    market_slug TEXT PRIMARY KEY,
                    open_time TEXT NOT NULL,
                    reference_price REAL NOT NULL,
                    reference_ts TEXT NOT NULL,
                    source TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def record_tick(self, tick: ChainlinkPriceTick) -> None:
        ts = tick.ts.astimezone(UTC).isoformat()
        with self.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {CHAINLINK_TABLE} (ts, price, raw_json, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ts) DO UPDATE SET
                    price=excluded.price,
                    raw_json=excluded.raw_json
                """,
                (ts, tick.price, _json(tick.raw), _now_iso()),
            )

    def latest_tick(self, *, max_age_seconds: float | None = None) -> ChainlinkPriceTick | None:
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT ts, price, raw_json FROM {CHAINLINK_TABLE} ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        tick = _tick_from_row(row)
        if tick is None:
            return None
        if max_age_seconds is not None:
            age = (_now_utc() - tick.ts.astimezone(UTC)).total_seconds()
            if age > max_age_seconds:
                return None
        return tick

    def reference_for_market(
        self,
        *,
        market_slug: str,
        open_time: datetime | None,
        tolerance_seconds: float = 120.0,
    ) -> ChainlinkPriceTick | None:
        cached = self.cached_reference(market_slug)
        if cached is not None:
            return cached
        if open_time is None:
            return None
        open_utc = open_time.astimezone(UTC)
        lower = (open_utc - timedelta(seconds=tolerance_seconds)).isoformat()
        upper = (open_utc + timedelta(seconds=tolerance_seconds)).isoformat()
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT ts, price, raw_json
                FROM {CHAINLINK_TABLE}
                WHERE ts >= ? AND ts <= ?
                ORDER BY ABS((julianday(ts) - julianday(?)) * 86400.0) ASC
                LIMIT 1
                """,
                (lower, upper, open_utc.isoformat()),
            ).fetchone()
        tick = _tick_from_row(row)
        if tick is None:
            return None
        self.record_reference(market_slug=market_slug, open_time=open_utc, tick=tick)
        return tick

    def cached_reference(self, market_slug: str) -> ChainlinkPriceTick | None:
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT reference_ts AS ts, reference_price AS price, raw_json FROM {REFERENCE_TABLE} WHERE market_slug = ?",
                (market_slug,),
            ).fetchone()
        return _tick_from_row(row)

    def record_reference(self, *, market_slug: str, open_time: datetime, tick: ChainlinkPriceTick) -> None:
        with self.connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {REFERENCE_TABLE}
                    (market_slug, open_time, reference_price, reference_ts, source, raw_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_slug) DO UPDATE SET
                    reference_price=excluded.reference_price,
                    reference_ts=excluded.reference_ts,
                    raw_json=excluded.raw_json
                """,
                (
                    market_slug,
                    open_time.astimezone(UTC).isoformat(),
                    tick.price,
                    tick.ts.astimezone(UTC).isoformat(),
                    "polymarket_chainlink_rtds",
                    _json(tick.raw),
                    _now_iso(),
                ),
            )


class ChainlinkRTDSBackgroundClient:
    def __init__(
        self,
        *,
        store: ChainlinkPriceStore,
        url: str = CHAINLINK_RTDS_URL,
        symbol: str = CHAINLINK_BTC_SYMBOL,
        reconnect_delay_seconds: float = 2.0,
    ) -> None:
        self.store = store
        self.url = url
        self.symbol = symbol
        self.reconnect_delay_seconds = reconnect_delay_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="polymarket-chainlink-rtds", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        asyncio.run(self._run_forever())

    async def _run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self._connect_once()
            except Exception:
                await asyncio.sleep(self.reconnect_delay_seconds)

    async def _connect_once(self) -> None:
        subscribe = {
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": "crypto_prices_chainlink",
                    "type": "*",
                    "filters": "",
                }
            ],
        }
        async with websockets.connect(self.url, ping_interval=None) as ws:
            await ws.send(json.dumps(subscribe))
            last_ping = time.monotonic()
            while not self._stop.is_set():
                if time.monotonic() - last_ping >= 5:
                    await ws.send("PING")
                    last_ping = time.monotonic()
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                ticks = parse_chainlink_rtds_message(message)
                for tick in ticks:
                    self.store.record_tick(tick)


def parse_chainlink_rtds_message(message: str | bytes) -> list[ChainlinkPriceTick]:
    if isinstance(message, bytes):
        message = message.decode("utf-8", errors="replace")
    if message in {"", "PONG", "PING"}:
        return []
    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, Mapping):
        return []
    if payload.get("topic") != "crypto_prices_chainlink":
        return []
    inner = payload.get("payload")
    if not isinstance(inner, Mapping):
        return []
    data = inner.get("data")
    if isinstance(data, list):
        ticks = []
        for item in data:
            if isinstance(item, Mapping):
                tick = _tick_from_payload(payload, item)
                if tick is not None:
                    ticks.append(tick)
        return ticks
    tick = _tick_from_payload(payload, inner)
    return [tick] if tick is not None else []


def _tick_from_payload(message: Mapping[str, Any], inner: Mapping[str, Any]) -> ChainlinkPriceTick | None:
    symbol = str(inner.get("symbol") or message.get("symbol") or CHAINLINK_BTC_SYMBOL).lower()
    if symbol != CHAINLINK_BTC_SYMBOL:
        return None
    value = _float_or_none(inner.get("value"))
    if value is None:
        return None
    ts_ms = _float_or_none(inner.get("timestamp")) or _float_or_none(message.get("timestamp"))
    ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC) if ts_ms is not None else _now_utc()
    return ChainlinkPriceTick(ts=ts, price=value, raw=dict(message))


def _tick_from_row(row: sqlite3.Row | None) -> ChainlinkPriceTick | None:
    if row is None:
        return None
    try:
        ts = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00")).astimezone(UTC)
        raw = json.loads(str(row["raw_json"] or "{}"))
        return ChainlinkPriceTick(ts=ts, price=float(row["price"]), raw=raw if isinstance(raw, dict) else {})
    except Exception:
        return None


def _float_or_none(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _now_iso() -> str:
    return _now_utc().replace(microsecond=0).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
