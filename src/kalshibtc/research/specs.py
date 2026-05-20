"""Research-only candidate spec validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar


class SpecValidationError(ValueError):
    """Raised when a candidate strategy spec is not safe or reviewable."""


@dataclass(frozen=True)
class CandidateSpec:
    """A validated, JSON-serializable research candidate spec.

    This is intentionally dependency-light and data-only: it validates JSON-shaped
    values without importing or executing any strategy code.
    """

    name: str
    economic_story: str
    mechanism: str
    parameters: dict[str, Any]
    falsification: str
    created_at: str
    parent_id: str

    REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = (
        "name",
        "economic_story",
        "mechanism",
        "parameters",
        "falsification",
        "created_at",
        "parent_id",
    )

    @classmethod
    def from_mapping(cls, mapping: dict[str, Any]) -> CandidateSpec:
        if not isinstance(mapping, dict):
            raise SpecValidationError("candidate spec must be a JSON object")

        for field in cls.REQUIRED_FIELDS:
            if field not in mapping:
                raise SpecValidationError(f"missing required field: {field}")

        allowed = set(cls.REQUIRED_FIELDS)
        unknown = sorted(set(mapping) - allowed)
        if unknown:
            raise SpecValidationError(f"unknown field: {unknown[0]}")

        values: dict[str, Any] = {}
        for field in cls.REQUIRED_FIELDS:
            value = mapping[field]
            if field == "parameters":
                if not isinstance(value, dict):
                    raise SpecValidationError("parameters must be a JSON object")
                _assert_json_value(value, field)
                values[field] = dict(value)
                continue

            if not isinstance(value, str) or not value.strip():
                raise SpecValidationError(f"{field} must be a non-empty string")
            values[field] = value

        _validate_created_at(values["created_at"])
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "economic_story": self.economic_story,
            "mechanism": self.mechanism,
            "parameters": self.parameters,
            "falsification": self.falsification,
            "created_at": self.created_at,
            "parent_id": self.parent_id,
        }

    def to_canonical_json(self) -> str:
        """Return stable, sorted-key JSON for reviewable artifacts."""

        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def load_candidate_spec(payload: str | bytes | bytearray) -> CandidateSpec:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SpecValidationError(f"invalid JSON: {exc.msg}") from exc

    if not isinstance(parsed, dict):
        raise SpecValidationError("candidate spec must be a JSON object")
    return CandidateSpec.from_mapping(parsed)


def _validate_created_at(value: str) -> None:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SpecValidationError("created_at must be an ISO-8601 timestamp") from exc


def _assert_json_value(value: Any, path: str) -> None:
    if value is None or isinstance(value, str | int | float | bool):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise SpecValidationError(f"{path} contains a non-string key")
            _assert_json_value(item, f"{path}.{key}")
        return
    raise SpecValidationError(f"{path} contains a non-JSON value")
