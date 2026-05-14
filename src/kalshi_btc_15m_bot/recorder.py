from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import now_utc, parse_ts

SNAPSHOT_DB_FILENAME = "realtime-snapshots-1s.sqlite3"
VOLUME_TODO = "Kalshi trade/volume websocket fields are not available in the current stream payload."

SNAPSHOT_COLUMNS = [
    "ts",
    "market_ticker",
    "market_open_time",
    "market_close_time",
    "market_expiration_time",
    "seconds_to_close",
    "minutes_to_close",
    "time_bucket",
    "btc_price",
    "strike",
    "target_price",
    "distance_from_strike",
    "abs_distance_from_strike",
    "is_above_strike",
    "distance_pct",
    "strike_crossed_recently",
    "seconds_since_last_strike_cross",
    "btc_velocity_10s",
    "slope_10s",
    "btc_velocity_30s",
    "slope_30s",
    "btc_velocity_60s",
    "slope_60s",
    "distance_velocity_30s",
    "distance_expanding",
    "yes_bid",
    "yes_ask",
    "no_bid",
    "no_ask",
    "yes_mid",
    "no_mid",
    "spread",
    "yes_spread",
    "no_spread",
    "min_spread",
    "yes_bid_depth",
    "yes_ask_depth",
    "no_bid_depth",
    "no_ask_depth",
    "top_book_json",
    "orderbook_sequence",
    "orderbook_crossed",
    "orderbook_warning",
    "market_implied_yes",
    "probability_yes",
    "probability_no",
    "probability_delta_30s",
    "probability_delta_60s",
    "model_probability_yes",
    "edge_yes",
    "edge_no",
    "best_side",
    "cumulative_volume",
    "volume_delta_1s",
    "volume_delta_10s",
    "volume_delta_60s",
    "volume_todo",
    "recent_trade_count",
    "last_trade_price",
    "last_trade_side",
    "execution_action",
    "execution_side",
    "execution_confidence",
    "execution_regime",
    "execution_reason",
    "execution_blocked_by_json",
    "raw_state_json",
    "created_at",
]


def snapshot_db_path(config: Any) -> Path:
    configured = getattr(config, "realtime_snapshots_path", None)
    if configured is not None:
        return Path(configured)
    data_dir = Path(getattr(config, "data_dir", "data"))
    return data_dir / SNAPSHOT_DB_FILENAME


class RealtimeSnapshotRecorder:
    """SQLite writer for replayable one-second websocket state snapshots.

    The recorder is read-only with respect to Kalshi: it only persists the already
    computed stream-state payload and never touches order submission code.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS realtime_snapshots_1s (
                    ts TEXT NOT NULL,
                    market_ticker TEXT NOT NULL,
                    market_open_time TEXT,
                    market_close_time TEXT,
                    market_expiration_time TEXT,
                    seconds_to_close REAL,
                    minutes_to_close REAL,
                    time_bucket TEXT,
                    btc_price REAL,
                    strike REAL,
                    target_price REAL,
                    distance_from_strike REAL,
                    abs_distance_from_strike REAL,
                    is_above_strike INTEGER,
                    distance_pct REAL,
                    strike_crossed_recently INTEGER,
                    seconds_since_last_strike_cross REAL,
                    btc_velocity_10s REAL,
                    slope_10s REAL,
                    btc_velocity_30s REAL,
                    slope_30s REAL,
                    btc_velocity_60s REAL,
                    slope_60s REAL,
                    distance_velocity_30s REAL,
                    distance_expanding INTEGER,
                    yes_bid REAL,
                    yes_ask REAL,
                    no_bid REAL,
                    no_ask REAL,
                    yes_mid REAL,
                    no_mid REAL,
                    spread REAL,
                    yes_spread REAL,
                    no_spread REAL,
                    min_spread REAL,
                    yes_bid_depth REAL,
                    yes_ask_depth REAL,
                    no_bid_depth REAL,
                    no_ask_depth REAL,
                    top_book_json TEXT,
                    orderbook_sequence INTEGER,
                    orderbook_crossed INTEGER,
                    orderbook_warning TEXT,
                    market_implied_yes REAL,
                    probability_yes REAL,
                    probability_no REAL,
                    probability_delta_30s REAL,
                    probability_delta_60s REAL,
                    model_probability_yes REAL,
                    edge_yes REAL,
                    edge_no REAL,
                    best_side TEXT,
                    cumulative_volume REAL,
                    volume_delta_1s REAL,
                    volume_delta_10s REAL,
                    volume_delta_60s REAL,
                    volume_todo TEXT,
                    recent_trade_count INTEGER,
                    last_trade_price REAL,
                    last_trade_side TEXT,
                    execution_action TEXT,
                    execution_side TEXT,
                    execution_confidence REAL,
                    execution_regime TEXT,
                    execution_reason TEXT,
                    execution_blocked_by_json TEXT NOT NULL,
                    raw_state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (market_ticker, ts)
                );
                CREATE INDEX IF NOT EXISTS idx_realtime_snapshots_1s_ts
                    ON realtime_snapshots_1s(ts);
                CREATE INDEX IF NOT EXISTS idx_realtime_snapshots_1s_market_close
                    ON realtime_snapshots_1s(market_ticker, seconds_to_close);
                """
            )
            existing_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(realtime_snapshots_1s)")
            }
            for column in ("yes_spread", "no_spread", "min_spread"):
                if column not in existing_columns:
                    conn.execute(f"ALTER TABLE realtime_snapshots_1s ADD COLUMN {column} REAL")

    def record_snapshot(self, payload: Mapping[str, Any]) -> bool:
        row = snapshot_row_from_payload(payload)
        placeholders = ", ".join("?" for _ in SNAPSHOT_COLUMNS)
        columns = ", ".join(SNAPSHOT_COLUMNS)
        values = tuple(row[column] for column in SNAPSHOT_COLUMNS)
        update_columns = [column for column in SNAPSHOT_COLUMNS if column not in {"market_ticker", "ts"}]
        updates = ", ".join(f"{column}=excluded.{column}" for column in update_columns)
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                INSERT INTO realtime_snapshots_1s ({columns}) VALUES ({placeholders})
                ON CONFLICT(market_ticker, ts) DO UPDATE SET {updates}
                """,
                values,
            )
        return cursor.rowcount == 1


def snapshot_row_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    ts = _snapshot_second(payload)
    btc_price = _first_float(payload, "btc_price", "current_price")
    target_price = _first_float(payload, "target_price", "strike")
    distance = _first_float(payload, "distance_from_strike", "distance_to_target")
    if distance is None and btc_price is not None and target_price is not None:
        distance = btc_price - target_price
    abs_distance = _first_float(payload, "abs_distance_from_strike", "abs_distance_to_target")
    if abs_distance is None and distance is not None:
        abs_distance = abs(distance)
    is_above = _first_bool(payload, "is_above_strike")
    if is_above is None and distance is not None:
        is_above = distance > 0

    yes_bid = _float_or_none(payload.get("yes_bid"))
    yes_ask = _float_or_none(payload.get("yes_ask"))
    no_bid = _float_or_none(payload.get("no_bid"))
    no_ask = _float_or_none(payload.get("no_ask"))
    yes_mid = _first_float(payload, "yes_mid")
    if yes_mid is None and yes_bid is not None and yes_ask is not None:
        yes_mid = (yes_bid + yes_ask) / 2.0
    no_mid = _first_float(payload, "no_mid")
    if no_mid is None and no_bid is not None and no_ask is not None:
        no_mid = (no_bid + no_ask) / 2.0
    yes_spread = _first_float(payload, "yes_spread")
    if yes_spread is None:
        yes_spread = _side_spread(bid=yes_bid, ask=yes_ask)
    no_spread = _first_float(payload, "no_spread")
    if no_spread is None:
        no_spread = _side_spread(bid=no_bid, ask=no_ask)
    min_spread = _first_float(payload, "min_spread")
    if min_spread is None:
        spread_candidates = [value for value in (yes_spread, no_spread) if value is not None]
        min_spread = min(spread_candidates) if spread_candidates else None
    spread = _first_float(payload, "spread", "best_spread")
    if spread is None:
        spread = min_spread

    seconds_to_close = _float_or_none(payload.get("seconds_to_close"))
    raw_execution = payload.get("execution_decision")
    execution: Mapping[str, Any] = raw_execution if isinstance(raw_execution, Mapping) else {}
    blocked_by = _blocked_by(execution)
    volume_keys = {
        "cumulative_volume",
        "market_volume",
        "volume",
        "volume_delta_1s",
        "volume_delta_10s",
        "volume_delta_60s",
        "recent_trade_count",
        "last_trade_price",
        "last_trade_side",
    }
    volume_todo = payload.get("volume_todo")
    if volume_todo is None and not any(key in payload for key in volume_keys):
        volume_todo = VOLUME_TODO

    warnings = _string_list(payload.get("warnings"))
    orderbook_crossed = _first_bool(payload, "orderbook_crossed")
    if orderbook_crossed is None and "orderbook_valid" in payload:
        orderbook_valid = _bool_or_none(payload.get("orderbook_valid"))
        orderbook_crossed = None if orderbook_valid is None else not orderbook_valid
    orderbook_warning = None
    if orderbook_crossed:
        orderbook_warning = "invalid_orderbook_quotes"
    elif "invalid_orderbook_quotes" in warnings:
        orderbook_warning = "invalid_orderbook_quotes"

    return {
        "ts": ts,
        "market_ticker": str(payload.get("market_ticker") or "UNKNOWN"),
        "market_open_time": _iso_or_none(payload.get("market_open_time")),
        "market_close_time": _iso_or_none(payload.get("market_close_time")),
        "market_expiration_time": _iso_or_none(
            payload.get("market_expiration_time") or payload.get("market_expected_expiration_time")
        ),
        "seconds_to_close": seconds_to_close,
        "minutes_to_close": _float_or_none(payload.get("minutes_to_close"))
        if payload.get("minutes_to_close") is not None
        else (seconds_to_close / 60.0 if seconds_to_close is not None else None),
        "time_bucket": str(payload.get("time_bucket") or _time_bucket(seconds_to_close)),
        "btc_price": btc_price,
        "strike": target_price,
        "target_price": target_price,
        "distance_from_strike": distance,
        "abs_distance_from_strike": abs_distance,
        "is_above_strike": _bool_to_int(is_above),
        "distance_pct": _first_float(payload, "distance_pct", "distance_to_target_pct"),
        "strike_crossed_recently": _bool_to_int(_first_bool(payload, "strike_crossed_recently")),
        "seconds_since_last_strike_cross": _float_or_none(
            payload.get("seconds_since_last_strike_cross")
        ),
        "btc_velocity_10s": _float_or_none(payload.get("btc_velocity_10s")),
        "slope_10s": _first_float(payload, "slope_10s", "btc_velocity_10s"),
        "btc_velocity_30s": _float_or_none(payload.get("btc_velocity_30s")),
        "slope_30s": _first_float(payload, "slope_30s", "btc_velocity_30s"),
        "btc_velocity_60s": _float_or_none(payload.get("btc_velocity_60s")),
        "slope_60s": _first_float(payload, "slope_60s", "btc_velocity_60s"),
        "distance_velocity_30s": _float_or_none(payload.get("distance_velocity_30s")),
        "distance_expanding": _bool_to_int(_first_bool(payload, "distance_expanding")),
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "yes_mid": yes_mid,
        "no_mid": no_mid,
        "spread": spread,
        "yes_spread": yes_spread,
        "no_spread": no_spread,
        "min_spread": min_spread,
        "yes_bid_depth": _float_or_none(payload.get("yes_bid_depth")),
        "yes_ask_depth": _float_or_none(payload.get("yes_ask_depth")),
        "no_bid_depth": _float_or_none(payload.get("no_bid_depth")),
        "no_ask_depth": _float_or_none(payload.get("no_ask_depth")),
        "top_book_json": _json_field(
            payload.get("top_book") or payload.get("top_book_json") or payload.get("raw_top_book_json")
        ),
        "orderbook_sequence": _int_or_none(payload.get("orderbook_sequence") or payload.get("orderbook_seq")),
        "orderbook_crossed": _bool_to_int(orderbook_crossed),
        "orderbook_warning": orderbook_warning,
        "market_implied_yes": _float_or_none(payload.get("market_implied_yes")),
        "probability_yes": _float_or_none(payload.get("probability_yes")),
        "probability_no": _float_or_none(payload.get("probability_no")),
        "probability_delta_30s": _float_or_none(payload.get("probability_delta_30s")),
        "probability_delta_60s": _float_or_none(payload.get("probability_delta_60s")),
        "model_probability_yes": _float_or_none(payload.get("model_probability_yes")),
        "edge_yes": _first_float(payload, "edge_yes", "probability_edge_yes"),
        "edge_no": _first_float(payload, "edge_no", "probability_edge_no"),
        "best_side": _string_or_none(payload.get("best_side") if "best_side" in payload else payload.get("best_ev_side")),
        "cumulative_volume": _first_float(payload, "cumulative_volume", "market_volume", "volume"),
        "volume_delta_1s": _float_or_none(payload.get("volume_delta_1s")),
        "volume_delta_10s": _float_or_none(payload.get("volume_delta_10s")),
        "volume_delta_60s": _float_or_none(payload.get("volume_delta_60s")),
        "volume_todo": _string_or_none(volume_todo),
        "recent_trade_count": _int_or_none(payload.get("recent_trade_count")),
        "last_trade_price": _float_or_none(payload.get("last_trade_price")),
        "last_trade_side": _string_or_none(payload.get("last_trade_side")),
        "execution_action": _string_or_none(execution.get("action")) or "NO_TRADE",
        "execution_side": _string_or_none(execution.get("side")) or "NONE",
        "execution_confidence": _float_or_none(execution.get("confidence")),
        "execution_regime": _string_or_none(execution.get("regime") or payload.get("regime")),
        "execution_reason": _string_or_none(execution.get("reason")),
        "execution_blocked_by_json": _json_dumps(blocked_by),
        "raw_state_json": _json_dumps(payload),
        "created_at": now_utc().replace(microsecond=0).isoformat(),
    }


def _snapshot_second(payload: Mapping[str, Any]) -> str:
    raw_ts = payload.get("ts") or payload.get("as_of") or payload.get("btc_ts")
    parsed = _parse_datetime(raw_ts) or now_utc()
    return parsed.astimezone(UTC).replace(microsecond=0).isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            return parse_ts(value.strip())
        except (TypeError, ValueError):
            return None
    return None


def _iso_or_none(value: Any) -> str | None:
    parsed = _parse_datetime(value)
    if parsed is not None:
        return parsed.astimezone(UTC).isoformat()
    text = _string_or_none(value)
    return text


def _time_bucket(seconds_to_close: float | None) -> str:
    if seconds_to_close is None:
        return "unknown"
    if seconds_to_close <= 30:
        return "final_seconds"
    if seconds_to_close <= 180:
        return "late"
    if seconds_to_close <= 600:
        return "middle"
    return "early"


def _side_spread(*, bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    return ask - bid


def _first_float(payload: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in payload:
            value = _float_or_none(payload.get(key))
            if value is not None:
                return value
    return None


def _first_bool(payload: Mapping[str, Any], *keys: str) -> bool | None:
    for key in keys:
        if key in payload:
            value = _bool_or_none(payload.get(key))
            if value is not None:
                return value
    return None


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n"}:
            return False
    return None


def _bool_to_int(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [str(item) for item in value]
    return []


def _blocked_by(execution: Mapping[str, Any]) -> list[str]:
    raw = execution.get("blocked_by")
    if isinstance(raw, Sequence) and not isinstance(raw, str):
        return [str(item) for item in raw]
    return []


def _json_field(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return _json_dumps(value)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
