from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path


def test_legacy_15m_package_is_archive_only() -> None:
    spec = importlib.util.find_spec("kalshi_btc_15m_bot")

    assert spec is None
    assert Path("archive/legacy-15m/src/kalshi_btc_15m_bot/streaming.py").is_file()


def test_console_scripts_are_feed_replay_paper_dashboard_validation_and_research_only() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())

    assert pyproject["project"]["scripts"] == {
        "kbtc-feed": "kalshibtc.record_1s_snapshots:main",
        "kbtc-feed-derived-columns": "kalshibtc.feed_derived_columns:main",
        "kbtc-replay": "kalshibtc.replay.cli:main",
        "kbtc-dashboard": "kalshibtc.dashboard:main",
        "kbtc-probability-dataset": "kalshibtc.probability.dataset_cli:main",
        "kbtc-paper": "kalshibtc.paper_signal_executor:main",
        "kbtc-research-journal": "kalshibtc.research.journal:main",
        "kbtc-auto-strategy-creator": "kalshibtc.research.auto_strategy_creator:main",
        "kbtc-poly-fill-validate": "kalshibtc.polymarket_fill_validation:main",
        "polymarket-btc-15m-recorder": "kalshibtc.polymarket_btc_15m_recorder:main",
    }
