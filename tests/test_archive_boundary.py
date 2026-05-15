from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path


def test_legacy_15m_package_is_archived_off_active_python_path() -> None:
    assert importlib.util.find_spec("kalshi_btc_15m_bot") is None


def test_only_active_console_script_is_1s_paper_executor() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())

    assert pyproject["project"]["scripts"] == {
        "kbtc15-1s-paper": "kalshibtc.paper_signal_executor:main"
    }


def test_legacy_archive_is_present_for_reference() -> None:
    archive_root = Path("archive/legacy-15m")

    assert (archive_root / "README.md").is_file()
    assert (archive_root / "src/kalshi_btc_15m_bot").is_dir()
