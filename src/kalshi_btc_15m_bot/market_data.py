from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import pandas as pd
import requests

from .config import MarketDataConfig
from .models import Candle, now_utc


class MarketDataProvider(Protocol):
    def fetch_candles(self, lookback_days: int) -> list[Candle]: ...

    def current_price(self) -> float: ...


@dataclass
class CoinbaseDataProvider:
    product_id: str = "BTC-USD"
    granularity_seconds: int = 900
    timeout_seconds: int = 20

    base_url: str = "https://api.exchange.coinbase.com"

    def fetch_candles(self, lookback_days: int) -> list[Candle]:
        end = now_utc()
        start = end - timedelta(days=lookback_days)
        max_span = timedelta(seconds=self.granularity_seconds * 300)
        rows: list[list[float]] = []
        chunk_end = end
        session = requests.Session()
        session.headers.update({"User-Agent": "kalshi-btc-15m-paper-bot/0.1"})

        while chunk_end > start:
            chunk_start = max(start, chunk_end - max_span)
            response = session.get(
                f"{self.base_url}/products/{self.product_id}/candles",
                params={
                    "granularity": self.granularity_seconds,
                    "start": _iso(chunk_start),
                    "end": _iso(chunk_end),
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            if isinstance(data, list):
                rows.extend(data)
            chunk_end = chunk_start
            # Be polite to Coinbase while still keeping one-shot scans quick.
            time.sleep(0.04)

        candles: dict[int, Candle] = {}
        for row in rows:
            # Coinbase: [time, low, high, open, close, volume]
            if len(row) < 6:
                continue
            ts = datetime.fromtimestamp(int(row[0]), tz=UTC)
            candles[int(row[0])] = Candle(
                ts=ts,
                open=float(row[3]),
                high=float(row[2]),
                low=float(row[1]),
                close=float(row[4]),
                volume=float(row[5]),
            )
        return [candles[key] for key in sorted(candles)]

    def current_price(self) -> float:
        response = requests.get(
            f"{self.base_url}/products/{self.product_id}/ticker", timeout=self.timeout_seconds
        )
        response.raise_for_status()
        return float(response.json()["price"])


@dataclass
class BinanceDataProvider:
    symbol: str = "BTCUSDT"
    granularity_seconds: int = 900
    timeout_seconds: int = 20

    base_url: str = "https://api.binance.com"

    def fetch_candles(self, lookback_days: int) -> list[Candle]:
        start = now_utc() - timedelta(days=lookback_days)
        end = now_utc()
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        rows: list[list[object]] = []
        session = requests.Session()
        interval = _binance_interval(self.granularity_seconds)

        while start_ms < end_ms:
            response = session.get(
                f"{self.base_url}/api/v3/klines",
                params={
                    "symbol": self.symbol,
                    "interval": interval,
                    "startTime": start_ms,
                    "endTime": end_ms,
                    "limit": 1000,
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            if not data:
                break
            rows.extend(data)
            start_ms = int(data[-1][6]) + 1
            time.sleep(0.04)

        candles: dict[int, Candle] = {}
        for row in rows:
            # Binance: open_time, open, high, low, close, volume, close_time, ...
            ts_ms = int(row[0])
            candles[ts_ms] = Candle(
                ts=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
        return [candles[key] for key in sorted(candles)]

    def current_price(self) -> float:
        response = requests.get(
            f"{self.base_url}/api/v3/ticker/price",
            params={"symbol": self.symbol},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return float(response.json()["price"])


def provider_from_config(config: MarketDataConfig) -> MarketDataProvider:
    provider = config.provider.lower()
    if provider == "coinbase":
        return CoinbaseDataProvider(
            product_id=config.product_id,
            granularity_seconds=config.granularity_seconds,
            timeout_seconds=config.request_timeout_seconds,
        )
    if provider == "binance":
        return BinanceDataProvider(
            symbol=config.symbol,
            granularity_seconds=config.granularity_seconds,
            timeout_seconds=config.request_timeout_seconds,
        )
    raise ValueError(f"Unsupported market_data.provider: {config.provider}")


def candles_to_frame(
    candles: list[Candle], *, granularity_seconds: int = 900, drop_incomplete: bool = True
) -> pd.DataFrame:
    rows = [
        {
            "date": c.ts,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
        }
        for c in candles
    ]
    if not rows:
        raise ValueError("No candles available")
    df = pd.DataFrame(rows).drop_duplicates(subset=["date"]).sort_values("date")
    df = df.set_index("date")
    if drop_incomplete:
        cutoff = now_utc() - timedelta(seconds=granularity_seconds + 5)
        df = df[df.index <= cutoff]
    if len(df) < 50:
        raise ValueError(f"Not enough completed candles after filtering: {len(df)}")
    return df


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _binance_interval(granularity_seconds: int) -> str:
    mapping = {60: "1m", 300: "5m", 900: "15m", 3600: "1h", 86400: "1d"}
    if granularity_seconds not in mapping:
        raise ValueError(f"Unsupported Binance granularity: {granularity_seconds}")
    return mapping[granularity_seconds]
