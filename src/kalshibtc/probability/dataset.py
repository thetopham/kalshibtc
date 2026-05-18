from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import pandas as pd  # type: ignore[import-untyped]
except Exception:  # pragma: no cover - pandas is optional for parquet only
    pd = None

from ..datafeed.models import OrderBookSnapshot, Tick
from ..market.contract import ContractWindow
from ..market.state import MarketState
from .features import extract_probability_features

FEATURE_COLUMNS = [
    "distance_to_strike",
    "seconds_to_close",
    "atr",
    "atr_slope",
    "realized_volatility",
    "ema_slope",
    "vwap_slope",
    "recent_momentum",
    "distance_from_vwap",
    "wickiness",
    "range_expansion",
    "orderbook_imbalance",
    "z_score",
]

BASE_COLUMNS = [
    "venue",
    "market_ticker",
    "ts",
    "strike",
    "btc_price",
    "seconds_to_close",
    "final_outcome",
    "final_outcome_yes",
    "realized_move_into_close",
    "market_implied_probability",
    "split_key",
    "regime_label",
]


@dataclass(frozen=True)
class DatasetExportSummary:
    rows: int
    markets: int
    output_path: Path
    format: str


def export_probability_dataset(
    *,
    feed_db: Path,
    venue: str,
    output_path: Path,
    from_ts: str | None = None,
    to_ts: str | None = None,
    output_format: str | None = None,
) -> DatasetExportSummary:
    rows = _load_snapshot_rows(feed_db, from_ts=from_ts, to_ts=to_ts)
    dataset = build_probability_dataset_rows(rows, venue=venue)
    fmt = (output_format or output_path.suffix.lstrip(".") or "csv").lower()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        _write_csv(output_path, dataset)
    elif fmt == "parquet":
        _write_parquet(output_path, dataset)
    else:
        raise ValueError(f"unsupported probability dataset format: {fmt}")
    return DatasetExportSummary(
        rows=len(dataset),
        markets=len({str(row["market_ticker"]) for row in dataset}),
        output_path=output_path,
        format=fmt,
    )


def build_probability_dataset_rows(rows: Sequence[Mapping[str, Any] | sqlite3.Row], *, venue: str) -> list[dict[str, Any]]:
    final_by_market = _final_snapshot_by_market(rows)
    market_order = sorted(final_by_market, key=lambda ticker: str(_field(final_by_market[ticker], "market_close_time") or ""))
    split_by_market = _split_keys(market_order)
    output: list[dict[str, Any]] = []
    for row in rows:
        market = str(_field(row, "market_ticker") or "")
        if not market or market not in final_by_market:
            continue
        state = _row_to_state(row)
        final_row = final_by_market[market]
        final_price = _as_float(_field(final_row, "btc_price"), state.price)
        final_outcome_yes = final_price > state.strike
        features = extract_probability_features(state)
        row_out: dict[str, Any] = {
            "venue": venue,
            "market_ticker": market,
            "ts": _field(row, "ts"),
            "strike": state.strike,
            "btc_price": state.price,
            "seconds_to_close": state.seconds_to_close,
            "final_outcome": "yes" if final_outcome_yes else "no",
            "final_outcome_yes": final_outcome_yes,
            "realized_move_into_close": final_price - state.price,
            "market_implied_probability": _market_implied_probability(row),
            "split_key": split_by_market.get(market, "train"),
            "regime_label": classify_regime(asdict(features), row),
        }
        for key in FEATURE_COLUMNS:
            row_out[key] = getattr(features, key)
        output.append(row_out)
    return output


def load_probability_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".parquet":
        if pd is None:
            raise RuntimeError("pandas is required to load parquet probability datasets")
        return pd.read_parquet(path).to_dict(orient="records")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def classify_regime(features: Mapping[str, Any], row: Mapping[str, Any] | sqlite3.Row | None = None) -> str:
    yes_bid = _as_float(_field(row, "yes_bid") if row is not None else None, 0.0)
    yes_ask = _as_float(_field(row, "yes_ask") if row is not None else None, 0.0)
    no_bid = _as_float(_field(row, "no_bid") if row is not None else None, 0.0)
    no_ask = _as_float(_field(row, "no_ask") if row is not None else None, 0.0)
    if yes_ask - yes_bid > 0.2 or no_ask - no_bid > 0.2:
        return "low_liquidity"
    if _as_float(features.get("atr_slope"), 0.0) > 0.5 or _as_float(features.get("range_expansion"), 1.0) >= 1.5:
        return "volatility_expansion"
    if _as_float(features.get("wickiness"), 0.0) >= 0.7:
        return "chop"
    if abs(_as_float(features.get("ema_slope"), 0.0)) > 1.0 or abs(_as_float(features.get("recent_momentum"), 0.0)) > 1.0:
        return "trend"
    return "chop"


def _load_snapshot_rows(feed_db: Path, *, from_ts: str | None, to_ts: str | None) -> list[sqlite3.Row]:
    where: list[str] = []
    params: list[str] = []
    if from_ts:
        where.append("ts >= ?")
        params.append(from_ts)
    if to_ts:
        where.append("ts <= ?")
        params.append(to_ts)
    sql = "SELECT * FROM realtime_snapshots_1s"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts ASC, market_ticker ASC"
    with sqlite3.connect(f"file:{feed_db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return list(conn.execute(sql, params))


def _final_snapshot_by_market(rows: Sequence[Mapping[str, Any] | sqlite3.Row]) -> dict[str, Mapping[str, Any] | sqlite3.Row]:
    final: dict[str, Mapping[str, Any] | sqlite3.Row] = {}
    for row in rows:
        market = str(_field(row, "market_ticker") or "")
        if not market:
            continue
        final[market] = row
    return final


def _split_keys(markets: Sequence[str]) -> dict[str, str]:
    total = len(markets)
    if total <= 1:
        return {market: "test" for market in markets}
    train_cut = max(1, int(total * 0.70))
    validation_cut = max(train_cut, int(total * 0.85))
    if total >= 3 and validation_cut == train_cut:
        validation_cut = train_cut + 1
    split: dict[str, str] = {}
    for index, market in enumerate(markets):
        if index < train_cut:
            split[market] = "train"
        elif index < validation_cut:
            split[market] = "validation"
        else:
            split[market] = "test"
    if "test" not in split.values() and markets:
        split[markets[-1]] = "test"
    return split


def _row_to_state(row: Mapping[str, Any] | sqlite3.Row) -> MarketState:
    raw = _json_or_empty(_field(row, "raw_json"))
    for key in ("market_ticker", "market_open_time", "market_close_time", "strike", "target_price", "seconds_to_close", "btc_velocity_30s", "slope_30s"):
        value = _field(row, key)
        if value is not None:
            raw[key] = value
    ts = _parse_dt(_field(row, "ts"))
    contract = ContractWindow(
        ticker=str(_field(row, "market_ticker") or ""),
        strike=_as_float(_field(row, "strike") or _field(row, "target_price"), 0.0),
        open_time=_parse_dt(_field(row, "market_open_time")) if _field(row, "market_open_time") else None,
        close_time=_parse_dt(_field(row, "market_close_time")),
    )
    book = OrderBookSnapshot(
        ts=ts,
        market_ticker=contract.ticker,
        yes_bid=_optional_float(row, "yes_bid"),
        yes_ask=_optional_float(row, "yes_ask"),
        no_bid=_optional_float(row, "no_bid"),
        no_ask=_optional_float(row, "no_ask"),
        sequence=_optional_int(row, "orderbook_sequence"),
        raw=raw,
    )
    tick = Tick(
        ts=ts,
        price=_as_float(_field(row, "btc_price"), 0.0),
        source="probability_dataset",
        symbol="BTC-USD",
        raw=raw,
    )
    return MarketState(
        tick=tick,
        orderbook=book,
        contract=contract,
        slope_30s=_optional_float(row, "slope_30s"),
    )


def _market_implied_probability(row: Mapping[str, Any] | sqlite3.Row) -> float | None:
    yes_ask = _optional_float(row, "yes_ask")
    yes_bid = _optional_float(row, "yes_bid")
    if yes_ask is not None:
        return yes_ask
    if yes_bid is not None:
        return yes_bid
    return None


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    columns = [*BASE_COLUMNS, *[col for col in FEATURE_COLUMNS if col != "seconds_to_close"]]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_parquet(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if pd is None:
        raise RuntimeError("pandas is required to write parquet probability datasets")
    pd.DataFrame(rows).to_parquet(path, index=False)


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _field(item: Mapping[str, Any] | sqlite3.Row | None, name: str) -> Any:
    if item is None:
        return None
    if isinstance(item, Mapping):
        return item.get(name)
    try:
        return item[name]
    except (KeyError, IndexError):
        return None


def _optional_float(row: Mapping[str, Any] | sqlite3.Row, name: str) -> float | None:
    value = _field(row, name)
    if value is None or value == "":
        return None
    return _as_float(value, 0.0)


def _optional_int(row: Mapping[str, Any] | sqlite3.Row, name: str) -> int | None:
    value = _field(row, name)
    if value is None or value == "":
        return None
    return int(value)


def _as_float(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_or_empty(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
