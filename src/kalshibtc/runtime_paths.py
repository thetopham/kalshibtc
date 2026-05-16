from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

PREFERRED_SNAPSHOT_DB = Path("runtime/snapshots/realtime-snapshots-1s.sqlite3")
PREFERRED_RESULTS_DB = Path("runtime/results/paper-results-1s.sqlite3")
LEGACY_SNAPSHOT_DB = Path("data/realtime-snapshots-1s.sqlite3")
LEGACY_RESULTS_DB = Path("data-live-prod/paper-results-1s.sqlite3")

SNAPSHOT_DB_ENV_VARS = ("KALSHIBTC_1S_SNAPSHOT_DB", "KALSHIBTC_SNAPSHOT_DB")
RESULTS_DB_ENV_VARS = ("KALSHIBTC_1S_RESULTS_DB", "KALSHIBTC_RESULTS_DB")


def resolve_snapshot_db(
    value: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    return _resolve_runtime_path(
        value,
        env_names=SNAPSHOT_DB_ENV_VARS,
        preferred=PREFERRED_SNAPSHOT_DB,
        legacy=LEGACY_SNAPSHOT_DB,
        env=env,
    )


def resolve_results_db(
    value: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    return _resolve_runtime_path(
        value,
        env_names=RESULTS_DB_ENV_VARS,
        preferred=PREFERRED_RESULTS_DB,
        legacy=LEGACY_RESULTS_DB,
        env=env,
    )


def _resolve_runtime_path(
    value: str | Path | None,
    *,
    env_names: Sequence[str],
    preferred: Path,
    legacy: Path,
    env: Mapping[str, str] | None,
) -> Path:
    if value not in (None, ""):
        return Path(value)
    values = os.environ if env is None else env
    for name in env_names:
        env_value = values.get(name)
        if env_value:
            return Path(env_value)
    if preferred.exists():
        return preferred
    if legacy.exists():
        return legacy
    return preferred
