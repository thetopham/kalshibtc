from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def dollars(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class KalshiMarket:
    ticker: str
    event_ticker: str
    title: str
    status: str
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    last_price: float
    target_price: float | None
    open_time: datetime | None
    close_time: datetime | None
    expected_expiration_time: datetime | None
    volume: float
    liquidity: float
    open_interest: float
    rules_primary: str = ""
    rules_secondary: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> KalshiMarket:
        return cls(
            ticker=str(raw.get("ticker", "")),
            event_ticker=str(raw.get("event_ticker", "")),
            title=str(raw.get("title", "")),
            status=str(raw.get("status", "")),
            yes_bid=dollars(raw.get("yes_bid_dollars")),
            yes_ask=dollars(raw.get("yes_ask_dollars")),
            no_bid=dollars(raw.get("no_bid_dollars")),
            no_ask=dollars(raw.get("no_ask_dollars")),
            last_price=dollars(raw.get("last_price_dollars")),
            target_price=dollars(raw.get("floor_strike"), math.nan),
            open_time=parse_ts(raw.get("open_time")),
            close_time=parse_ts(raw.get("close_time")),
            expected_expiration_time=parse_ts(raw.get("expected_expiration_time")),
            volume=dollars(raw.get("volume_fp")),
            liquidity=dollars(raw.get("liquidity_dollars")),
            open_interest=dollars(raw.get("open_interest_fp")),
            rules_primary=str(raw.get("rules_primary") or ""),
            rules_secondary=str(raw.get("rules_secondary") or ""),
            raw=raw,
        )

    @property
    def yes_mid(self) -> float:
        if self.yes_bid and self.yes_ask:
            return (self.yes_bid + self.yes_ask) / 2
        return self.last_price or self.yes_ask or self.yes_bid or 0.5

    @property
    def no_mid(self) -> float:
        if self.no_bid and self.no_ask:
            return (self.no_bid + self.no_ask) / 2
        return 1.0 - self.yes_mid

    def seconds_to_close(self, at: datetime | None = None) -> float | None:
        if not self.close_time:
            return None
        return (self.close_time - (at or now_utc())).total_seconds()

    def to_jsonable(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ["open_time", "close_time", "expected_expiration_time"]:
            if data[key] is not None:
                data[key] = data[key].isoformat()
        return data


@dataclass(frozen=True)
class ModelInfo:
    name: str
    trained: bool
    samples: int
    test_accuracy: float | None
    brier: float | None
    weight: float

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


@dataclass(frozen=True)
class Prediction:
    prediction_id: str
    created_at: datetime
    market: KalshiMarket
    current_price: float
    probability_yes: float
    probability_no: float
    action: str
    side: str | None
    edge: float
    confidence: float
    stake_dollars: float
    reasons: list[str]
    model_info: ModelInfo
    feature_snapshot: dict[str, float]

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "prediction_id": self.prediction_id,
            "created_at": self.created_at.isoformat(),
            "market_ticker": self.market.ticker,
            "event_ticker": self.market.event_ticker,
            "market_close_time": self.market.close_time.isoformat() if self.market.close_time else None,
            "target_price": self.market.target_price,
            "current_price": self.current_price,
            "probability_yes": self.probability_yes,
            "probability_no": self.probability_no,
            "action": self.action,
            "side": self.side,
            "edge": self.edge,
            "confidence": self.confidence,
            "stake_dollars": self.stake_dollars,
            "yes_ask": self.market.yes_ask,
            "no_ask": self.market.no_ask,
            "yes_bid": self.market.yes_bid,
            "no_bid": self.market.no_bid,
            "model_info": asdict(self.model_info),
            "reasons": self.reasons,
            "feature_snapshot": self.feature_snapshot,
        }
