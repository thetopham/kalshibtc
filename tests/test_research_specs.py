import json

import pytest

from kalshibtc.research.specs import CandidateSpec, SpecValidationError, load_candidate_spec


def _valid_spec_dict(**overrides):
    spec = {
        "name": "late_window_spread_filter",
        "economic_story": "Late-window BTC contracts may overreact when spreads briefly widen.",
        "mechanism": "Trade only near expiry when top-of-book spread is tight and BTC momentum confirms.",
        "parameters": {
            "entry_seconds_before_close": 180,
            "max_spread_cents": 4,
            "momentum_threshold": 0.0025,
        },
        "falsification": "Reject if replay Sharpe is below 1.0 or max drawdown exceeds $25.",
        "created_at": "2026-05-16T06:30:00Z",
        "parent_id": "research-seed-001",
    }
    spec.update(overrides)
    return spec


def test_valid_json_candidate_spec_loads_into_reviewable_dataclass():
    payload = json.dumps(_valid_spec_dict())

    spec = load_candidate_spec(payload)

    assert isinstance(spec, CandidateSpec)
    assert spec.name == "late_window_spread_filter"
    assert spec.parameters["max_spread_cents"] == 4
    assert spec.parent_id == "research-seed-001"


def test_canonical_json_serialization_sorts_keys_for_reviewable_artifacts():
    spec = CandidateSpec.from_mapping(_valid_spec_dict())

    canonical = spec.to_canonical_json()

    assert canonical == json.dumps(json.loads(canonical), sort_keys=True, separators=(",", ":"))
    assert list(json.loads(canonical).keys()) == [
        "created_at",
        "economic_story",
        "falsification",
        "mechanism",
        "name",
        "parameters",
        "parent_id",
    ]


@pytest.mark.parametrize(
    ("field", "bad_value", "reason"),
    [
        ("name", "", "name must be a non-empty string"),
        ("economic_story", "   ", "economic_story must be a non-empty string"),
        ("mechanism", 123, "mechanism must be a non-empty string"),
        ("parameters", [], "parameters must be a JSON object"),
        ("falsification", None, "falsification must be a non-empty string"),
        ("created_at", "not-a-date", "created_at must be an ISO-8601 timestamp"),
        ("parent_id", "", "parent_id must be a non-empty string"),
    ],
)
def test_invalid_specs_fail_with_clear_reasons(field, bad_value, reason):
    invalid = _valid_spec_dict(**{field: bad_value})

    with pytest.raises(SpecValidationError, match=reason):
        CandidateSpec.from_mapping(invalid)


def test_missing_required_field_fails_with_clear_reason():
    invalid = _valid_spec_dict()
    del invalid["mechanism"]

    with pytest.raises(SpecValidationError, match="missing required field: mechanism"):
        CandidateSpec.from_mapping(invalid)


def test_unknown_fields_are_rejected_to_keep_ai_output_reviewable():
    invalid = _valid_spec_dict(import_path="os.system")

    with pytest.raises(SpecValidationError, match="unknown field: import_path"):
        CandidateSpec.from_mapping(invalid)


def test_json_loader_rejects_non_object_payloads():
    with pytest.raises(SpecValidationError, match="candidate spec must be a JSON object"):
        load_candidate_spec('["not", "an", "object"]')


def test_json_loader_reports_malformed_json_without_importing_code():
    with pytest.raises(SpecValidationError, match="invalid JSON"):
        load_candidate_spec('{"name":')
