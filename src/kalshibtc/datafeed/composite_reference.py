from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Any


@dataclass(frozen=True)
class VenueObservation:
    venue: str
    ts: datetime
    price: float


@dataclass(frozen=True)
class CompositeReferenceConfig:
    window_seconds: int = 60
    max_observation_age_seconds: int | None = None
    outlier_max_deviation_pct: float = 10.0


@dataclass(frozen=True)
class CompositeReference:
    as_of: datetime
    price: float
    source: str
    window_seconds: int
    included_venues: tuple[str, ...]
    included_observation_count: int
    excluded_observation_count: int
    provenance: dict[str, Any]


def compute_composite_reference(
    observations: Sequence[VenueObservation],
    *,
    as_of: datetime,
    expected_venues: Sequence[str] = (),
    config: CompositeReferenceConfig | None = None,
) -> CompositeReference | None:
    """Approximate a Kalshi/CF-style 60s multi-venue BTC reference.

    This is a deterministic local research approximation, not an official CF
    Benchmarks or Kalshi settlement value. It averages fresh sampled spot prices
    across venues, excluding stale/window-miss observations and gross outliers.
    """
    cfg = config or CompositeReferenceConfig()
    max_age = cfg.max_observation_age_seconds or cfg.window_seconds
    start_age = cfg.window_seconds
    exclusions: dict[str, list[str]] = defaultdict(list)
    windowed: list[VenueObservation] = []

    for observation in observations:
        age_seconds = (as_of - observation.ts).total_seconds()
        if age_seconds < 0 or age_seconds >= start_age:
            exclusions["outside_window"].append(observation.venue)
            continue
        if age_seconds > max_age:
            exclusions["stale"].append(observation.venue)
            continue
        if observation.price <= 0:
            exclusions["invalid_price"].append(observation.venue)
            continue
        windowed.append(observation)

    if not windowed:
        return None

    prices = [observation.price for observation in windowed]
    center = median(prices)
    included: list[VenueObservation] = []
    for observation in windowed:
        deviation_pct = abs(observation.price - center) / center * 100.0 if center else 0.0
        if deviation_pct > cfg.outlier_max_deviation_pct:
            exclusions["outlier"].append(observation.venue)
            continue
        included.append(observation)

    if not included:
        return None

    included_venues = tuple(sorted({observation.venue for observation in included}))
    price = sum(observation.price for observation in included) / len(included)
    expected = set(expected_venues)
    missing_venues = sorted(expected - set(included_venues) - set(exclusions.get("stale", [])))
    stale_venues = sorted(set(exclusions.get("stale", [])))
    warnings: list[str] = []
    source = "composite_60s_reference"
    if len(included_venues) == 1:
        source = "single_venue" if len(observations) == 1 else "composite_60s_reference"
        warnings.append("single venue fallback" if len(observations) == 1 else "single fresh venue")
    if missing_venues:
        warnings.append("missing expected venue data")
    if stale_venues:
        warnings.append("stale venue data excluded")

    return CompositeReference(
        as_of=as_of,
        price=price,
        source=source,
        window_seconds=cfg.window_seconds,
        included_venues=included_venues,
        included_observation_count=len(included),
        excluded_observation_count=sum(len(venues) for venues in exclusions.values()),
        provenance={
            "label": "approximation_not_official_cf_or_kalshi",
            "method": "fresh_non_outlier_arithmetic_mean",
            "missing_venues": missing_venues,
            "stale_venues": stale_venues,
            "warnings": warnings,
            "excluded_venues_by_reason": {reason: sorted(venues) for reason, venues in exclusions.items()},
        },
    )
