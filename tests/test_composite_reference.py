from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kalshibtc.datafeed.composite_reference import (
    CompositeReferenceConfig,
    VenueObservation,
    compute_composite_reference,
)


def _ts(seconds: int) -> datetime:
    return datetime(2026, 5, 15, 12, 0, seconds, tzinfo=UTC)


def test_composite_reference_averages_fresh_non_outlier_venue_observations_over_60s() -> None:
    observations = [
        VenueObservation(venue="coinbase", ts=_ts(0), price=100_000.0),
        VenueObservation(venue="coinbase", ts=_ts(30), price=100_060.0),
        VenueObservation(venue="kraken", ts=_ts(30), price=100_040.0),
        VenueObservation(venue="bitstamp", ts=_ts(59), price=100_080.0),
        VenueObservation(venue="coinbase", ts=_ts(0) - timedelta(seconds=1), price=99_000.0),
        VenueObservation(venue="badprint", ts=_ts(59), price=150_000.0),
    ]

    reference = compute_composite_reference(observations, as_of=_ts(59))

    assert reference.price == pytest.approx(100_045.0)
    assert reference.source == "composite_60s_reference"
    assert reference.window_seconds == 60
    assert reference.as_of == _ts(59)
    assert reference.included_venues == ("bitstamp", "coinbase", "kraken")
    assert reference.included_observation_count == 4
    assert reference.excluded_observation_count == 2
    assert reference.provenance["label"] == "approximation_not_official_cf_or_kalshi"
    assert "badprint" in reference.provenance["excluded_venues_by_reason"]["outlier"]
    assert "coinbase" in reference.provenance["excluded_venues_by_reason"]["outside_window"]


def test_composite_reference_excludes_stale_or_missing_venue_data_with_warnings() -> None:
    reference = compute_composite_reference(
        [
            VenueObservation(venue="coinbase", ts=_ts(59), price=100_000.0),
            VenueObservation(venue="kraken", ts=_ts(10), price=100_010.0),
        ],
        as_of=_ts(59),
        expected_venues=("coinbase", "kraken", "bitstamp"),
        config=CompositeReferenceConfig(max_observation_age_seconds=30),
    )

    assert reference.price == 100_000.0
    assert reference.source == "composite_60s_reference"
    assert reference.included_venues == ("coinbase",)
    assert reference.provenance["missing_venues"] == ["bitstamp"]
    assert reference.provenance["stale_venues"] == ["kraken"]
    assert "single fresh venue" in reference.provenance["warnings"]


def test_composite_reference_single_venue_fallback_is_labeled() -> None:
    reference = compute_composite_reference(
        [VenueObservation(venue="coinbase", ts=_ts(59), price=100_123.0)],
        as_of=_ts(59),
    )

    assert reference.price == 100_123.0
    assert reference.source == "single_venue"
    assert reference.included_venues == ("coinbase",)
    assert "single venue fallback" in reference.provenance["warnings"]


def test_composite_reference_returns_none_when_all_observations_are_unusable() -> None:
    reference = compute_composite_reference(
        [VenueObservation(venue="coinbase", ts=_ts(0), price=100_000.0)],
        as_of=_ts(59),
        config=CompositeReferenceConfig(max_observation_age_seconds=10),
    )

    assert reference is None
