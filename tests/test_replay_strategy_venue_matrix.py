from __future__ import annotations

import json
from pathlib import Path

from test_feed_replay_architecture import _write_feed_db

from kalshibtc.strategy.registry import strategy_names
from scripts import replay_strategy_venue_matrix as matrix


def test_matrix_skips_polymarket_only_strategies_on_kalshi(tmp_path: Path) -> None:
    kalshi = tmp_path / "kalshi.sqlite3"
    poly = tmp_path / "poly.sqlite3"
    _write_feed_db(kalshi)
    _write_feed_db(poly)
    out = tmp_path / "matrix"

    assert matrix.main([
        "--runs-dir", str(tmp_path / "runs"),
        "--kalshi-feed-db", str(kalshi),
        "--polymarket-feed-db", str(poly),
        "--strategy", "hedge_volatility_v0",
        "--strategy", "no_trade_baseline",
        "--run-prefix", "test-matrix",
        "--out", str(out),
        "--json",
    ]) == 0

    artifact = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert {row["venue"] for row in artifact["all_results"]} == {"kalshi", "polymarket"}
    assert {row["strategy"] for row in artifact["all_results"]} == {"no_trade_baseline", "hedge_volatility_v0"}
    assert {row["venue"] for row in artifact["all_results"] if row["strategy"] == "hedge_volatility_v0"} == {"polymarket"}
    assert {
        (row["venue"], row["strategy"], row["reason"])
        for row in artifact["skipped"]
    } == {("kalshi", "hedge_volatility_v0", "venue_ineligible")}
    assert artifact["eligible_run_count"] == 3
    assert artifact["valid_run_count"] == 3


def test_matrix_default_eligibility_counts_all_strategies() -> None:
    eligible = sum(
        1
        for venue in matrix.VENUES
        for strategy in strategy_names()
        if venue in matrix.strategy_metadata(strategy).allowed_venues
    )
    skipped = sum(
        1
        for venue in matrix.VENUES
        for strategy in strategy_names()
        if venue not in matrix.strategy_metadata(strategy).allowed_venues
    )
    assert eligible == 38
    assert skipped == 6
