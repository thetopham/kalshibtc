from __future__ import annotations

import pytest

from kalshi_btc_15m_bot.config import load_config

LIVE_TOML = '''
trading_mode = "live"
enable_live_orders = true
data_dir = "data"

[live]
environment = "demo"
base_url = "https://external-api.demo.kalshi.co/trade-api/v2"
acknowledgement = "I_UNDERSTAND_KALSHI_DEMO_ORDERS"
auto_trade = true
max_order_dollars = 5.0
max_contracts = 5
max_open_positions = 1
max_daily_orders = 3
max_daily_loss_dollars = 10.0
min_cash_reserve_dollars = 25.0
min_liquidity_dollars = 50.0
max_spread = 0.10
take_profit_pct = 0.20
stop_loss_pct = -0.30
force_close_seconds_to_close = 30
min_seconds_between_orders = 60
time_in_force = "immediate_or_cancel"
subaccount = 0
'''


def test_config_refuses_live_orders_in_paper_mode(tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "bad.toml"
    cfg.write_text('trading_mode = "paper"\nenable_live_orders = true\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="enable_live_orders=true requires trading_mode='live'"):
        load_config(cfg)


def test_live_config_requires_literal_acknowledgement(tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "bad.toml"
    cfg.write_text(LIVE_TOML.replace("I_UNDERSTAND_KALSHI_DEMO_ORDERS", "yes"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KALSHI_API_KEY_ID", "demo-key-id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_FILE", str(tmp_path / "kalshi.key"))

    with pytest.raises(ValueError, match="acknowledgement"):
        load_config(cfg)


def test_live_config_requires_credentials_from_environment(tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "live.toml"
    cfg.write_text(LIVE_TOML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_FILE", raising=False)

    with pytest.raises(ValueError, match="KALSHI_API_KEY_ID"):
        load_config(cfg)


def test_live_config_accepts_demo_with_acknowledgement_caps_and_env(tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "live.toml"
    cfg.write_text(LIVE_TOML, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KALSHI_API_KEY_ID", "demo-key-id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_FILE", str(tmp_path / "kalshi.key"))

    loaded = load_config(cfg)

    assert loaded.trading_mode == "live"
    assert loaded.enable_live_orders is True
    assert loaded.live.environment == "demo"
    assert loaded.live.auto_trade is True
    assert loaded.live.max_order_dollars == 5.0


def test_production_live_config_requires_extra_ack_and_allow_flag(tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "live-prod.toml"
    cfg.write_text(
        LIVE_TOML.replace('environment = "demo"', 'environment = "production"').replace(
            'acknowledgement = "I_UNDERSTAND_KALSHI_DEMO_ORDERS"',
            'acknowledgement = "I_UNDERSTAND_THIS_SUBMITS_REAL_KALSHI_PRODUCTION_ORDERS"',
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KALSHI_API_KEY_ID", "prod-key-id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_FILE", str(tmp_path / "kalshi.key"))

    with pytest.raises(ValueError, match="allow_production"):
        load_config(cfg)

    cfg.write_text(cfg.read_text(encoding="utf-8") + "allow_production = true\n", encoding="utf-8")
    loaded = load_config(cfg)
    assert loaded.live.environment == "production"
    assert loaded.live.allow_production is True
