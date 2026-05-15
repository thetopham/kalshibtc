from __future__ import annotations

import json
import os
import sqlite3
import stat
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import BotConfig, load_config
from .dashboard import _dashboard_token_from_env, _validate_dashboard_auth
from .kalshi_client import KalshiPublicClient

DOCTOR_PROBE_PREFIX = ".doctor-"


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status != "error"

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "status": self.status,
            "message": self.message,
            "details": self.details,
        }


def check_config_load(
    config_path: str | Path | None,
    *,
    loader: Callable[[str | Path | None], BotConfig] = load_config,
) -> tuple[BotConfig | None, DoctorCheck]:
    try:
        config = loader(config_path)
    except Exception as exc:  # noqa: BLE001 - doctor should summarize operator failures.
        return None, DoctorCheck(
            name="config_load",
            status="error",
            message=f"Config failed to load: {_error_text(exc)}",
            details={"config_path": str(config_path) if config_path else None},
        )
    return config, DoctorCheck(
        name="config_load",
        status="ok",
        message="Config loaded successfully.",
        details={
            "config_path": str(config_path) if config_path else None,
            "trading_mode": config.trading_mode,
            "enable_live_orders": config.enable_live_orders,
            "data_dir": str(config.data_dir),
        },
    )


def check_data_dir_and_ledger_writable(config: BotConfig) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        probe = config.data_dir / f"{DOCTOR_PROBE_PREFIX}write-{uuid.uuid4().hex}"
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink()
    except Exception as exc:  # noqa: BLE001 - permissions vary by host.
        checks.append(
            DoctorCheck(
                name="data_dir_writable",
                status="error",
                message=f"data_dir is not writable: {_error_text(exc)}",
                details={"data_dir": str(config.data_dir)},
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="data_dir_writable",
                status="ok",
                message="data_dir is writable.",
                details={"data_dir": str(config.data_dir)},
            )
        )

    ledger_probe = config.data_dir / f"{DOCTOR_PROBE_PREFIX}ledger-{uuid.uuid4().hex}.sqlite3"
    try:
        if config.ledger_path.exists() and not os.access(config.ledger_path, os.W_OK):
            raise PermissionError(f"ledger file is not writable: {config.ledger_path}")
        config.data_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(ledger_probe) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE doctor_probe (id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO doctor_probe DEFAULT VALUES")
    except Exception as exc:  # noqa: BLE001 - permissions/filesystem errors should be reported.
        checks.append(
            DoctorCheck(
                name="ledger_writable",
                status="error",
                message=f"Ledger directory cannot create/write SQLite files: {_error_text(exc)}",
                details={"ledger_path": str(config.ledger_path), "probe_path": str(ledger_probe)},
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="ledger_writable",
                status="ok",
                message="Ledger directory can create and write SQLite files.",
                details={"ledger_path": str(config.ledger_path)},
            )
        )
    finally:
        _remove_sqlite_probe(ledger_probe)
    return checks


def check_kalshi_public_api(
    config: BotConfig,
    *,
    client_factory: Callable[[str, int], Any] = KalshiPublicClient,
) -> DoctorCheck:
    try:
        client = client_factory(
            config.kalshi.base_url,
            config.market_data.request_timeout_seconds,
        )
        markets = client.list_markets(
            series_ticker=config.kalshi.series_ticker,
            status=config.kalshi.market_status,
            limit=1,
        )
    except Exception as exc:  # noqa: BLE001 - network diagnostics should be concise.
        return DoctorCheck(
            name="kalshi_public_api",
            status="error",
            message=f"Kalshi public API is not reachable: {_error_text(exc)}",
            details={"base_url": config.kalshi.base_url, "series_ticker": config.kalshi.series_ticker},
        )
    status = "ok" if markets else "warning"
    message = (
        "Kalshi public API reachable."
        if markets
        else "Kalshi public API reachable, but no matching markets were returned."
    )
    return DoctorCheck(
        name="kalshi_public_api",
        status=status,
        message=message,
        details={
            "base_url": config.kalshi.base_url,
            "series_ticker": config.kalshi.series_ticker,
            "market_status": config.kalshi.market_status,
            "markets_seen": len(markets),
        },
    )


def check_websocket_credentials(env: Mapping[str, str] | None = None) -> DoctorCheck:
    values = os.environ if env is None else env
    api_key_id = values.get("KALSHI_API_KEY_ID")
    private_key_file = values.get("KALSHI_PRIVATE_KEY_FILE")
    if not api_key_id and not private_key_file:
        return DoctorCheck(
            name="websocket_credentials",
            status="warning",
            message=(
                "KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_FILE are not set; "
                "stream-state and dashboard --stream cannot connect to Kalshi's websocket."
            ),
        )
    missing = [
        name
        for name, value in {
            "KALSHI_API_KEY_ID": api_key_id,
            "KALSHI_PRIVATE_KEY_FILE": private_key_file,
        }.items()
        if not value
    ]
    if missing:
        return DoctorCheck(
            name="websocket_credentials",
            status="error",
            message=f"Partial Kalshi websocket credentials; missing {', '.join(missing)}.",
        )
    return DoctorCheck(
        name="websocket_credentials",
        status="ok",
        message="Kalshi websocket credential environment variables are present.",
        details={"private_key_file_configured": True},
    )


def check_private_key_file(private_key_file: str | Path | None) -> DoctorCheck:
    if not private_key_file:
        return DoctorCheck(
            name="private_key_file_permissions",
            status="warning",
            message="KALSHI_PRIVATE_KEY_FILE is not set; no private key file permissions to validate.",
        )
    path = Path(private_key_file).expanduser()
    if not path.exists():
        return DoctorCheck(
            name="private_key_file_permissions",
            status="error",
            message="Kalshi private key file configured by KALSHI_PRIVATE_KEY_FILE does not exist.",
            details={"private_key_file_configured": True},
        )
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return DoctorCheck(
            name="private_key_file_permissions",
            status="error",
            message="Cannot stat Kalshi private key file configured by KALSHI_PRIVATE_KEY_FILE.",
            details={"private_key_file_configured": True},
        )
    if mode & 0o077:
        return DoctorCheck(
            name="private_key_file_permissions",
            status="error",
            message="Kalshi private key file must be private; run: chmod 600 $KALSHI_PRIVATE_KEY_FILE",
            details={"private_key_file_configured": True, "mode": oct(mode)},
        )
    return DoctorCheck(
        name="private_key_file_permissions",
        status="ok",
        message="Kalshi private key file permissions are private.",
        details={"private_key_file_configured": True, "mode": oct(mode)},
    )


def check_dashboard_token_safety(
    *,
    host: str,
    token: str | None,
    env: Mapping[str, str] | None = None,
) -> DoctorCheck:
    values = os.environ if env is None else env
    resolved_token = _dashboard_token_from_env(token, env=values)
    try:
        _validate_dashboard_auth(host, resolved_token)
    except ValueError as exc:
        return DoctorCheck(
            name="dashboard_token_safety",
            status="error",
            message=_error_text(exc),
            details={"host": host, "token_configured": bool(resolved_token)},
        )
    return DoctorCheck(
        name="dashboard_token_safety",
        status="ok",
        message="Dashboard token binding rules are satisfied.",
        details={"host": host, "token_configured": bool(resolved_token)},
    )


def run_doctor(
    *,
    config_path: str | Path | None = None,
    dashboard_host: str = "127.0.0.1",
    dashboard_token: str | None = None,
    json_output: bool = False,
    emit: Callable[[str], None] | None = None,
) -> int:
    write = emit or print
    checks: list[DoctorCheck] = []
    config, config_check = check_config_load(config_path)
    checks.append(config_check)
    if config is not None:
        checks.extend(check_data_dir_and_ledger_writable(config))
        checks.append(check_kalshi_public_api(config))
    checks.append(check_websocket_credentials())
    checks.append(check_private_key_file(os.getenv("KALSHI_PRIVATE_KEY_FILE")))
    checks.append(
        check_dashboard_token_safety(
            host=dashboard_host,
            token=dashboard_token,
        )
    )

    payload = _doctor_payload(checks, config=config, dashboard_host=dashboard_host)
    write(json.dumps(payload, sort_keys=True, indent=2) if json_output else format_doctor_report(payload))
    return 1 if any(check.status == "error" for check in checks) else 0


def format_doctor_report(payload: Mapping[str, Any]) -> str:
    lines = ["kbtc15 doctor"]
    raw_summary = payload.get("summary")
    summary = raw_summary if isinstance(raw_summary, Mapping) else {}
    lines.append(
        "summary: "
        f"errors={summary.get('errors', 0)} warnings={summary.get('warnings', 0)} "
        f"ok={summary.get('ok', 0)}"
    )
    for check in payload.get("checks", []):
        if not isinstance(check, Mapping):
            continue
        lines.append(f"[{check.get('status')}] {check.get('name')}: {check.get('message')}")
    return "\n".join(lines)


def _doctor_payload(
    checks: list[DoctorCheck],
    *,
    config: BotConfig | None,
    dashboard_host: str,
) -> dict[str, Any]:
    return {
        "ok": not any(check.status == "error" for check in checks),
        "summary": {
            "ok": sum(1 for check in checks if check.status == "ok"),
            "warnings": sum(1 for check in checks if check.status == "warning"),
            "errors": sum(1 for check in checks if check.status == "error"),
        },
        "config": None
        if config is None
        else {
            "trading_mode": config.trading_mode,
            "enable_live_orders": config.enable_live_orders,
            "data_dir": str(config.data_dir),
            "ledger_path": str(config.ledger_path),
            "kalshi_base_url": config.kalshi.base_url,
        },
        "dashboard_host": dashboard_host,
        "checks": [check.to_jsonable() for check in checks],
    }


def _remove_sqlite_probe(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _error_text(exc: BaseException) -> str:
    text = str(exc).replace("\n", " ").replace("\r", " ").strip()
    return text or exc.__class__.__name__
