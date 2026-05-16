from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

DEFAULT_FEED_DB = Path("feed/kalshi-btc-1s.sqlite3")
DEFAULT_RUNS_DIR = Path("runs")
DEFAULT_LOG_DIR = Path("logs")

FEED_DB_ENV_VAR = "KALSHIBTC_FEED_DB"
RUNS_DIR_ENV_VAR = "KALSHIBTC_RUNS_DIR"
LOG_DIR_ENV_VAR = "KALSHIBTC_LOG_DIR"

# Backward-compatible aliases for imports that existed during the earlier 1s cleanup.
PREFERRED_SNAPSHOT_DB = DEFAULT_FEED_DB
PREFERRED_RESULTS_DB = DEFAULT_RUNS_DIR / "paper-results-1s.sqlite3"


def resolve_feed_db(
    value: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    return _resolve_path(value, env_name=FEED_DB_ENV_VAR, default=DEFAULT_FEED_DB, env=env)


def resolve_runs_dir(
    value: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    return _resolve_path(value, env_name=RUNS_DIR_ENV_VAR, default=DEFAULT_RUNS_DIR, env=env)


def resolve_log_dir(
    value: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    return _resolve_path(value, env_name=LOG_DIR_ENV_VAR, default=DEFAULT_LOG_DIR, env=env)


def resolve_snapshot_db(
    value: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Compatibility wrapper: snapshot DB is now the canonical feed DB."""
    return resolve_feed_db(value, env=env)


def resolve_results_db(
    value: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Compatibility wrapper for old paper executor callers.

    New replay/backtest runs should use resolve_runs_dir() and create immutable
    per-run result directories instead of one mutable results database.
    """
    if value not in (None, ""):
        return Path(value)
    values = os.environ if env is None else env
    env_value = values.get("KALSHIBTC_RESULTS_DB") or values.get("KALSHIBTC_1S_RESULTS_DB")
    if env_value:
        return Path(env_value)
    return PREFERRED_RESULTS_DB


def _resolve_path(
    value: str | Path | None,
    *,
    env_name: str,
    default: Path,
    env: Mapping[str, str] | None,
) -> Path:
    if value not in (None, ""):
        return Path(value)
    values = os.environ if env is None else env
    env_value = values.get(env_name)
    if env_value:
        return Path(env_value)
    return default
