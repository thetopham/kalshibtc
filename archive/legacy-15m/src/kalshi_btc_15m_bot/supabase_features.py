from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import requests

from .config import BotConfig, SupabaseFeatureConfig
from .models import parse_ts

DEFAULT_FEATURE_TABLE = "tv_datafeed_btc"
DEFAULT_FEATURE_SYMBOL = "BTCUSD"
DEFAULT_FEATURE_TIMEFRAME = 1
DEFAULT_MAX_FEATURE_AGE_SECONDS = 90

_NUMERIC_COLUMNS = (
    "o",
    "h",
    "l",
    "c",
    "v",
    "sma20",
    "sma50",
    "ema8",
    "ema21",
    "vwap",
    "atr",
    "bb_upper",
    "bb_middle",
    "bb_lower",
    "rsi",
    "macd",
    "macd_signal",
    "macd_hist",
    "stoch_k",
    "stoch_d",
    "obv",
    "fisher",
    "phobos_momentum",
    "vzo",
)

@dataclass(frozen=True)
class TradingViewBtcFeatures:
    symbol: str
    timeframe: int
    ts: datetime
    o: float
    h: float
    l: float  # noqa: E741 - mirrors TradingView/Supabase OHLC column name.
    c: float
    v: float
    sma20: float | None = None
    sma50: float | None = None
    ema8: float | None = None
    ema21: float | None = None
    vwap: float | None = None
    atr: float | None = None
    bb_upper: float | None = None
    bb_middle: float | None = None
    bb_lower: float | None = None
    rsi: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    stoch_k: float | None = None
    stoch_d: float | None = None
    obv: float | None = None
    fisher: float | None = None
    phobos_momentum: float | None = None
    vzo: float | None = None
    feature_age_seconds: float | None = None
    max_feature_age_seconds: float = DEFAULT_MAX_FEATURE_AGE_SECONDS
    is_fresh: bool = False
    current_price: float | None = None
    target_price: float | None = None
    seconds_to_close: float | None = None
    distance_to_target: float | None = None
    abs_distance_to_target: float | None = None
    atr_distance: float | None = None
    atr_to_close_estimate: float | None = None
    target_z: float | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> TradingViewBtcFeatures:
        ts = parse_ts(str(row.get("ts") or row.get("time") or row.get("timestamp") or ""))
        if ts is None:
            raise ValueError("Supabase feature row is missing a usable ts timestamp")
        values = {name: _nullable_float(row.get(name)) for name in _NUMERIC_COLUMNS}
        c = values.pop("c")
        o = values.pop("o")
        h = values.pop("h")
        low = values.pop("l")
        v = values.pop("v")
        if c is None or o is None or h is None or low is None or v is None:
            raise ValueError("Supabase feature row is missing required OHLCV fields")
        return cls(
            symbol=str(row.get("symbol") or DEFAULT_FEATURE_SYMBOL),
            timeframe=int(_nullable_float(row.get("timeframe")) or DEFAULT_FEATURE_TIMEFRAME),
            ts=ts,
            o=o,
            h=h,
            l=low,
            c=c,
            v=v,
            **values,
        )

    def with_target_context(
        self,
        *,
        now: datetime,
        current_price: float,
        target_price: float | None,
        seconds_to_close: float | None,
        max_feature_age_seconds: float = DEFAULT_MAX_FEATURE_AGE_SECONDS,
    ) -> TradingViewBtcFeatures:
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        else:
            now = now.astimezone(UTC)
        age = (now - self.ts).total_seconds()
        distance = current_price - target_price if target_price is not None else None
        abs_distance = abs(distance) if distance is not None else None
        atr = self.atr if self.atr is not None and self.atr > 0 else None
        atr_distance = abs_distance / atr if abs_distance is not None and atr else None
        close_seconds = seconds_to_close if seconds_to_close is not None and seconds_to_close > 0 else None
        atr_to_close_estimate = (
            atr * math.sqrt(close_seconds / 60.0) if atr is not None and close_seconds is not None else None
        )
        target_z = distance / close_seconds if distance is not None and close_seconds else None
        return replace(
            self,
            feature_age_seconds=age,
            max_feature_age_seconds=max_feature_age_seconds,
            is_fresh=0 <= age <= max_feature_age_seconds,
            current_price=current_price,
            target_price=target_price,
            seconds_to_close=seconds_to_close,
            distance_to_target=distance,
            abs_distance_to_target=abs_distance,
            atr_distance=atr_distance,
            atr_to_close_estimate=atr_to_close_estimate,
            target_z=target_z,
        )

    @property
    def stale(self) -> bool:
        return not self.is_fresh

    def to_jsonable(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source": "supabase_tv_datafeed",
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "ts": self.ts.isoformat(),
            "stale": self.stale,
            "is_fresh": self.is_fresh,
            "feature_age_seconds": self.feature_age_seconds,
            "max_feature_age_seconds": self.max_feature_age_seconds,
        }
        for name in _NUMERIC_COLUMNS:
            data[name] = getattr(self, name)
        data.update(
            {
                "current_price": self.current_price,
                "target_price": self.target_price,
                "seconds_to_close": self.seconds_to_close,
                "distance_to_target": self.distance_to_target,
                "abs_distance_to_target": self.abs_distance_to_target,
                "atr_distance": self.atr_distance,
                "atr_to_close_estimate": self.atr_to_close_estimate,
                "target_z": self.target_z,
            }
        )
        return data

    def to_feature_snapshot(self) -> dict[str, float]:
        snapshot: dict[str, float] = {}
        for name in _NUMERIC_COLUMNS:
            _add_finite(snapshot, f"tv_{name}", getattr(self, name))
        for name in (
            "feature_age_seconds",
            "max_feature_age_seconds",
            "current_price",
            "target_price",
            "seconds_to_close",
            "distance_to_target",
            "abs_distance_to_target",
            "atr_distance",
            "atr_to_close_estimate",
            "target_z",
        ):
            _add_finite(snapshot, f"tv_{name}", getattr(self, name))
        snapshot["tv_stale"] = 1.0 if self.stale else 0.0
        return snapshot

class SupabaseFeatureClient:
    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        table: str = DEFAULT_FEATURE_TABLE,
        symbol: str = DEFAULT_FEATURE_SYMBOL,
        timeframe: int = DEFAULT_FEATURE_TIMEFRAME,
        max_feature_age_seconds: float = DEFAULT_MAX_FEATURE_AGE_SECONDS,
        timeout_seconds: float = 10.0,
        session: Any | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.table = table
        self.symbol = symbol
        self.timeframe = timeframe
        self.max_feature_age_seconds = max_feature_age_seconds
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "apikey": api_key,
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            }
        )

    def fetch_latest_btc_1m(
        self,
        *,
        now: datetime,
        current_price: float,
        target_price: float | None,
        seconds_to_close: float | None,
    ) -> TradingViewBtcFeatures | None:
        response = self.session.get(
            f"{self.url}/rest/v1/{self.table}",
            params={
                "select": "*",
                "symbol": f"eq.{self.symbol}",
                "timeframe": f"eq.{self.timeframe}",
                "order": "ts.desc",
                "limit": "1",
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload:
            return None
        row = payload[0] if isinstance(payload, list) else payload
        if not isinstance(row, Mapping):
            raise ValueError("Supabase feature response did not contain an object row")
        return TradingViewBtcFeatures.from_row(row).with_target_context(
            now=now,
            current_price=current_price,
            target_price=target_price,
            seconds_to_close=seconds_to_close,
            max_feature_age_seconds=self.max_feature_age_seconds,
        )

def fetch_latest_btc_1m(
    client: SupabaseFeatureClient,
    *,
    now: datetime,
    current_price: float,
    target_price: float | None,
    seconds_to_close: float | None,
) -> TradingViewBtcFeatures | None:
    return client.fetch_latest_btc_1m(
        now=now,
        current_price=current_price,
        target_price=target_price,
        seconds_to_close=seconds_to_close,
    )

def feature_client_from_config(config: BotConfig) -> SupabaseFeatureClient | None:
    feature_cfg: SupabaseFeatureConfig = config.supabase_features
    if not feature_cfg.enabled:
        return None
    return SupabaseFeatureClient(
        url=feature_cfg.url,
        api_key=feature_cfg.api_key,
        table=feature_cfg.table,
        symbol=feature_cfg.symbol,
        timeframe=feature_cfg.timeframe,
        max_feature_age_seconds=feature_cfg.max_feature_age_seconds,
        timeout_seconds=feature_cfg.request_timeout_seconds,
    )

def _nullable_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None

def _add_finite(snapshot: dict[str, float], name: str, value: Any) -> None:
    numeric = _nullable_float(value)
    if numeric is not None:
        snapshot[name] = numeric
