from __future__ import annotations

import stat

from kalshi_btc_15m_bot.config import BotConfig
from kalshi_btc_15m_bot.doctor import (
    check_dashboard_token_safety,
    check_data_dir_and_ledger_writable,
    check_kalshi_public_api,
    check_private_key_file,
    check_websocket_credentials,
)


class FakeKalshiClient:
    def __init__(self, base_url: str, timeout_seconds: int) -> None:
        self.base_url = base_url
        self.timeout_seconds = timeout_seconds
        self.calls: list[tuple[str, str, int]] = []

    def list_markets(self, *, series_ticker: str, status: str | None, limit: int) -> list[object]:
        self.calls.append((series_ticker, status or "", limit))
        return [object()]


def _check_by_name(checks, name: str):
    return next(check for check in checks if check.name == name)


def test_data_dir_and_ledger_writability_checks_write_without_leaving_probe_files(tmp_path) -> None:
    config = BotConfig(data_dir=tmp_path / "kbtc15-data")

    checks = check_data_dir_and_ledger_writable(config)

    assert _check_by_name(checks, "data_dir_writable").ok is True
    assert _check_by_name(checks, "ledger_writable").ok is True
    assert config.data_dir.exists()
    assert not list(config.data_dir.glob(".doctor-*"))


def test_private_key_file_requires_private_permissions(tmp_path) -> None:
    key = tmp_path / "kalshi.key"
    key.write_text("fake-key", encoding="utf-8")
    key.chmod(0o644)

    check = check_private_key_file(key)

    assert check.ok is False
    assert check.status == "error"
    assert "chmod 600" in check.message

    key.chmod(0o600)
    ok_check = check_private_key_file(key)
    assert ok_check.ok is True
    assert stat.S_IMODE(key.stat().st_mode) == 0o600


def test_websocket_credentials_are_warning_when_absent_and_error_when_partial(tmp_path) -> None:
    missing = check_websocket_credentials({})
    assert missing.ok is True
    assert missing.status == "warning"
    assert "stream-state" in missing.message

    partial = check_websocket_credentials({"KALSHI_API_KEY_ID": "abc"})
    assert partial.ok is False
    assert partial.status == "error"
    assert "KALSHI_PRIVATE_KEY_FILE" in partial.message

    key = tmp_path / "kalshi.key"
    key.write_text("fake-key", encoding="utf-8")
    key.chmod(0o600)
    configured = check_websocket_credentials(
        {"KALSHI_API_KEY_ID": "abc", "KALSHI_PRIVATE_KEY_FILE": str(key)}
    )
    assert configured.ok is True
    assert configured.status == "ok"


def test_dashboard_token_safety_requires_token_for_non_loopback_bind() -> None:
    public_check = check_dashboard_token_safety(host="0.0.0.0", token=None, env={})
    assert public_check.ok is False
    assert public_check.status == "error"
    assert "requires" in public_check.message

    lan_check = check_dashboard_token_safety(host="192.168.1.50", token=None, env={})
    assert lan_check.ok is False

    loopback_check = check_dashboard_token_safety(host="127.0.0.1", token=None, env={})
    assert loopback_check.ok is True

    token_check = check_dashboard_token_safety(
        host="0.0.0.0", token=None, env={"KALSHI_BTC15M_DASHBOARD_TOKEN": "secret"}
    )
    assert token_check.ok is True


def test_public_api_reachability_uses_public_markets_endpoint() -> None:
    created: list[FakeKalshiClient] = []

    def make_client(base_url: str, timeout_seconds: int) -> FakeKalshiClient:
        client = FakeKalshiClient(base_url, timeout_seconds)
        created.append(client)
        return client

    config = BotConfig()
    check = check_kalshi_public_api(config, client_factory=make_client)

    assert check.ok is True
    assert created[0].base_url == config.kalshi.base_url
    assert created[0].calls == [(config.kalshi.series_ticker, config.kalshi.market_status, 1)]
