from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CalibrationBucket:
    lower: float
    upper: float
    count: int
    mean_probability: float
    observed_frequency: float


@dataclass(frozen=True)
class CalibrationReport:
    buckets: list[CalibrationBucket]
    expected_calibration_error: float


@dataclass(frozen=True)
class WalkForwardSplit:
    train: list[Mapping[str, Any]]
    test: list[Mapping[str, Any]]


def brier_score(probabilities: Sequence[float], labels: Sequence[bool]) -> float:
    _validate_same_length(probabilities, labels)
    if not probabilities:
        return 0.0
    return sum((float(p) - (1.0 if label else 0.0)) ** 2 for p, label in zip(probabilities, labels, strict=True)) / len(probabilities)


def log_loss(probabilities: Sequence[float], labels: Sequence[bool]) -> float:
    _validate_same_length(probabilities, labels)
    if not probabilities:
        return 0.0
    eps = 1e-15
    total = 0.0
    for p, label in zip(probabilities, labels, strict=True):
        p = min(1.0 - eps, max(eps, float(p)))
        total += -math.log(p if label else 1.0 - p)
    return total / len(probabilities)


def calibration_report(probabilities: Sequence[float], labels: Sequence[bool], *, bins: int = 10) -> CalibrationReport:
    _validate_same_length(probabilities, labels)
    if bins <= 0:
        raise ValueError("bins must be positive")
    buckets: list[CalibrationBucket] = []
    total = len(probabilities)
    ece = 0.0
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        members = []
        for p, label in zip(probabilities, labels, strict=True):
            p_float = float(p)
            in_bucket = lower <= p_float <= upper if index == bins - 1 else lower <= p_float < upper
            if in_bucket:
                members.append((p_float, bool(label)))
        if not members:
            buckets.append(CalibrationBucket(lower, upper, 0, 0.0, 0.0))
            continue
        mean_probability = sum(p for p, _ in members) / len(members)
        observed = sum(1.0 if label else 0.0 for _, label in members) / len(members)
        ece += (len(members) / total) * abs(mean_probability - observed) if total else 0.0
        buckets.append(CalibrationBucket(lower, upper, len(members), mean_probability, observed))
    return CalibrationReport(buckets=buckets, expected_calibration_error=ece)


def expected_calibration_error(probabilities: Sequence[float], labels: Sequence[bool], *, bins: int = 10) -> float:
    return calibration_report(probabilities, labels, bins=bins).expected_calibration_error


def walk_forward_splits(
    rows: Sequence[Mapping[str, Any]],
    *,
    train_markets: int,
    test_markets: int,
) -> list[WalkForwardSplit]:
    if train_markets <= 0 or test_markets <= 0:
        raise ValueError("train_markets and test_markets must be positive")
    markets = _ordered_markets(rows)
    splits: list[WalkForwardSplit] = []
    for start in range(0, len(markets) - train_markets - test_markets + 1):
        train_market_set = set(markets[start : start + train_markets])
        test_market_set = set(markets[start + train_markets : start + train_markets + test_markets])
        splits.append(
            WalkForwardSplit(
                train=[row for row in rows if str(row.get("market_ticker")) in train_market_set],
                test=[row for row in rows if str(row.get("market_ticker")) in test_market_set],
            )
        )
    return splits


def ev_capture_metrics(
    rows: Sequence[Mapping[str, Any]],
    *,
    edge_threshold: float,
    fee_rate: float,
    slippage: float,
) -> dict[str, float | int | None]:
    pnls: list[float] = []
    wins = 0
    for row in rows:
        p = _as_float(row.get("model_probability"), 0.0)
        market_p = _as_float(row.get("market_implied_probability"), 0.0)
        label_yes = _as_bool(row.get("final_outcome_yes"))
        yes_edge = p - market_p
        no_edge = (1.0 - p) - (1.0 - market_p)
        if yes_edge >= edge_threshold:
            pnl = (1.0 if label_yes else 0.0) - market_p - abs(fee_rate) * market_p - abs(slippage)
        elif no_edge >= edge_threshold:
            no_price = 1.0 - market_p
            pnl = (1.0 if not label_yes else 0.0) - no_price - abs(fee_rate) * no_price - abs(slippage)
        else:
            continue
        pnls.append(pnl)
        if pnl > 0:
            wins += 1
    total = sum(pnls)
    return {
        "trades": len(pnls),
        "wins": wins,
        "losses": len(pnls) - wins,
        "ev_capture": total,
        "mean_pnl": total / len(pnls) if pnls else 0.0,
        "sharpe": _sharpe(pnls),
    }


def implied_vs_realized_buckets(
    rows: Sequence[Mapping[str, Any]],
    *,
    probability_column: str = "market_implied_probability",
    label_column: str = "final_outcome_yes",
    bins: int = 10,
) -> list[CalibrationBucket]:
    probabilities = [_as_float(row.get(probability_column), 0.0) for row in rows]
    labels = [_as_bool(row.get(label_column)) for row in rows]
    return calibration_report(probabilities, labels, bins=bins).buckets


def _ordered_markets(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    first_ts: dict[str, str] = {}
    for row in rows:
        market = str(row.get("market_ticker"))
        ts = str(row.get("ts") or "")
        if market not in first_ts or ts < first_ts[market]:
            first_ts[market] = ts
    return [market for market, _ in sorted(first_ts.items(), key=lambda item: item[1])]


def _validate_same_length(probabilities: Sequence[float], labels: Sequence[bool]) -> None:
    if len(probabilities) != len(labels):
        raise ValueError("probabilities and labels must have same length")


def _as_float(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _sharpe(values: Sequence[float]) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return 0.0 if values[0] == 0 else (1.0 if values[0] > 0 else -1.0)
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    std = math.sqrt(variance)
    if std <= 0:
        return 0.0 if mean == 0 else (1.0 if mean > 0 else -1.0)
    return mean / std * math.sqrt(len(values))
