from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path


def test_previous_stream_dashboard_package_is_active_again() -> None:
    spec = importlib.util.find_spec("kalshi_btc_15m_bot")

    assert spec is not None
    assert spec.origin is not None
    assert "src/kalshi_btc_15m_bot" in spec.origin


def test_console_scripts_include_previous_dashboard_and_clean_1s_components() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())

    assert pyproject["project"]["scripts"] == {
        "kbtc15": "kalshi_btc_15m_bot.cli:main",
        "kbtc15-1s-dashboard": "kalshibtc.dashboard:main",
        "kbtc15-1s-paper": "kalshibtc.paper_signal_executor:main",
        "kbtc15-1s-recorder": "kalshibtc.record_1s_snapshots:main",
        "kbtc15-1s-status-dashboard": "kalshibtc.dashboard:main_status",
        "kbtc15-1s-stream-dashboard": "kalshibtc.dashboard:main_stream",
    }


def test_legacy_archive_is_present_for_reference() -> None:
    archive_root = Path("archive/legacy-15m")

    assert (archive_root / "README.md").is_file()
    assert (archive_root / "src/kalshi_btc_15m_bot").is_dir()
