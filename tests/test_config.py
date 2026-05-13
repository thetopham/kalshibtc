from __future__ import annotations

import pytest

from kalshi_btc_15m_bot.config import load_config


def test_config_refuses_live_mode(tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "bad.toml"
    cfg.write_text('trading_mode = "live"\nenable_live_orders = true\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        load_config(cfg)
