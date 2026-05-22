from __future__ import annotations

import json
from pathlib import Path

from kalshibtc.research.auto_strategy_creator import main


def test_auto_strategy_creator_dry_run_writes_reviewable_spec_and_report(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    rc = main(
        [
            "--dry-run",
            "--hypothesis",
            "brownian_edge_high_confidence",
            "--seed",
            "unit-test",
            "--runs-dir",
            str(tmp_path / "runs"),
            "--research-dir",
            str(tmp_path / "research"),
            "--run-id-prefix",
            "pytest",
            "--from",
            "2026-05-20T00:00:00Z",
            "--to",
            "2026-05-20T00:15:00Z",
        ]
    )

    assert rc == 0
    created = sorted((tmp_path / "research" / "auto_strategy_creator").glob("pytest-brownian_edge_high_confidence-*.json"))
    assert len(created) == 1
    spec = json.loads(created[0].read_text(encoding="utf-8"))
    assert spec["name"] == "brownian_edge_high_confidence"
    assert spec["parameters"]["replay_strategy"] == "strategy_probability_mm_v0"
    assert spec["parameters"]["risk"]["fill_timing"] == "next-tick"
    assert "live orders" not in spec["mechanism"].lower()

    report = created[0].with_suffix(".md")
    text = report.read_text(encoding="utf-8")
    assert "Safety boundary: replay/research only; no live orders." in text
    assert "Dry run only; replay was not executed." in text

    manifest = tmp_path / "research" / "auto_strategy_creator" / "manifest.csv"
    assert manifest.exists()
    assert "brownian_edge_high_confidence" in manifest.read_text(encoding="utf-8")
