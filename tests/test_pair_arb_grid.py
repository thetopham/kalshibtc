from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from test_live_strategy_paper import _iso
from test_kalshibtc_1s_paper_executor import _snapshot_db

from kalshibtc.execution.pair_grid_inventory import PairArbGridConfig, PairArbGridManager
from kalshibtc.live_strategy_paper import run_live_strategy_paper_once
from kalshibtc.strategy.registry import create_strategy, strategy_names


def _write_grid_snapshot_db(path: Path) -> None:
    close_time = datetime(2026, 5, 15, 12, 14, tzinfo=UTC)
    _snapshot_db(
        path,
        rows=[
            {
                "ts": _iso(10),
                "market_ticker": "KXBTC15M-GRID-A",
                "market_close_time": close_time.isoformat(),
                "btc_price": 100_000.0,
                "strike": 100_000.0,
                "btc_velocity_30s": 0.0,
                "yes_bid": 0.48,
                "yes_ask": 0.50,
                "no_bid": 0.48,
                "no_ask": 0.50,
            },
            {
                "ts": _iso(20),
                "market_ticker": "KXBTC15M-GRID-A",
                "market_close_time": close_time.isoformat(),
                "btc_price": 100_120.0,
                "strike": 100_000.0,
                "btc_velocity_30s": 2.0,
                "yes_bid": 0.70,
                "yes_ask": 0.72,
                "no_bid": 0.26,
                "no_ask": 0.28,
            },
            {
                "ts": _iso(30),
                "market_ticker": "KXBTC15M-GRID-A",
                "market_close_time": close_time.isoformat(),
                "btc_price": 99_900.0,
                "strike": 100_000.0,
                "btc_velocity_30s": -2.0,
                "yes_bid": 0.25,
                "yes_ask": 0.30,
                "no_bid": 0.66,
                "no_ask": 0.68,
            },
            {
                "ts": _iso(50),
                "market_ticker": "KXBTC15M-GRID-A",
                "market_close_time": close_time.isoformat(),
                "btc_price": 100_050.0,
                "strike": 100_000.0,
                "btc_velocity_30s": 1.0,
                "yes_bid": 0.62,
                "yes_ask": 0.66,
                "no_bid": 0.32,
                "no_ask": 0.34,
            },
        ],
    )


def test_pair_arb_grid_is_registered() -> None:
    assert "pair_arb_grid" in strategy_names()
    assert create_strategy("pair_arb_grid").name == "pair_arb_grid"


def test_grid_starts_tiny_on_both_sides_when_starter_cost_allowed() -> None:
    manager = PairArbGridManager(PairArbGridConfig(starter_pair_threshold=1.00, slippage=0.0))

    result = manager.on_book(
        event_key="grid-event",
        market_ticker="KXBTC15M-GRID-A",
        ts=datetime(2026, 5, 15, 12, 0, 10, tzinfo=UTC),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=830.0,
        distance_from_strike=0.0,
        above_ask=0.50,
        below_ask=0.50,
    )

    position = manager.positions["grid-event"]
    assert result.fills == 2
    assert position.held_above_qty == 5
    assert position.held_below_qty == 5
    assert position.pair_cost == 1.0
    assert position.max_capital_at_risk == 5.0
    assert position.locked_profit_to_max_capital_at_risk == 0.0


def test_grid_buys_cheaper_underweight_leg_only_if_projected_pair_cost_under_threshold() -> None:
    manager = PairArbGridManager(PairArbGridConfig(starter_pair_threshold=1.00, add_pair_threshold=0.95, slippage=0.0))
    manager.on_book(
        event_key="grid-event",
        market_ticker="KXBTC15M-GRID-A",
        ts=datetime(2026, 5, 15, 12, 0, 10, tzinfo=UTC),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=830.0,
        distance_from_strike=0.0,
        above_ask=0.50,
        below_ask=0.50,
    )

    add_below = manager.on_book(
        event_key="grid-event",
        market_ticker="KXBTC15M-GRID-A",
        ts=datetime(2026, 5, 15, 12, 0, 20, tzinfo=UTC),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=820.0,
        distance_from_strike=120.0,
        above_ask=0.72,
        below_ask=0.28,
    )

    position = manager.positions["grid-event"]
    assert add_below.fills == 1
    assert position.held_above_qty == 5
    assert position.held_below_qty == 10
    assert position.unpaired_directional_exposure == 5
    assert position.projected_pair_cost_for("ABOVE", 0.30, 5) <= 0.95

    add_above = manager.on_book(
        event_key="grid-event",
        market_ticker="KXBTC15M-GRID-A",
        ts=datetime(2026, 5, 15, 12, 0, 30, tzinfo=UTC),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=810.0,
        distance_from_strike=-100.0,
        above_ask=0.30,
        below_ask=0.68,
    )

    assert add_above.fills == 1
    assert position.held_above_qty == 10
    assert position.held_below_qty == 10
    assert position.pair_cost == 0.79
    assert position.locked_profit == 2.1
    assert position.locked_profit_to_max_capital_at_risk > 0


def test_grid_respects_imbalance_cap_and_stop_adding_near_expiry() -> None:
    manager = PairArbGridManager(PairArbGridConfig(max_imbalance_qty=5, slippage=0.0))
    manager.on_book(
        event_key="grid-event",
        market_ticker="KXBTC15M-GRID-A",
        ts=datetime(2026, 5, 15, 12, 0, 10, tzinfo=UTC),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=830.0,
        distance_from_strike=0.0,
        above_ask=0.50,
        below_ask=0.50,
    )
    manager.on_book(
        event_key="grid-event",
        market_ticker="KXBTC15M-GRID-A",
        ts=datetime(2026, 5, 15, 12, 0, 20, tzinfo=UTC),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=820.0,
        distance_from_strike=120.0,
        above_ask=0.72,
        below_ask=0.28,
    )
    blocked = manager.on_book(
        event_key="grid-event",
        market_ticker="KXBTC15M-GRID-A",
        ts=datetime(2026, 5, 15, 12, 0, 21, tzinfo=UTC),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=819.0,
        distance_from_strike=130.0,
        above_ask=0.73,
        below_ask=0.27,
    )
    near_expiry = manager.on_book(
        event_key="late-event",
        market_ticker="KXBTC15M-GRID-LATE",
        ts=datetime(2026, 5, 15, 12, 13, 10, tzinfo=UTC),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=50.0,
        distance_from_strike=20.0,
        above_ask=0.49,
        below_ask=0.49,
    )

    assert blocked.fills == 0
    assert blocked.reason == "max_imbalance_qty"
    assert near_expiry.fills == 0
    assert near_expiry.reason == "stop_adding_near_expiry"


def test_live_pair_arb_grid_writes_metrics_and_sqlite(tmp_path: Path) -> None:
    snapshot_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_grid_snapshot_db(snapshot_db)

    summary = run_live_strategy_paper_once(
        snapshot_db=snapshot_db,
        runs_dir=runs_dir,
        strategies=["pair_arb_grid"],
        limit=10,
        no_official_settlement=True,
    )

    payload = summary.strategies[0]
    assert payload["strategy"] == "pair_arb_grid"
    assert payload["pair_positions"] == 1
    assert payload["locked_profit"] > 0
    assert payload["locked_profit_to_max_capital_at_risk"] > 0
    assert payload["unpaired_directional_exposure"] <= 25

    results_db = runs_dir / "live" / "pair_arb_grid" / "results.sqlite3"
    with sqlite3.connect(results_db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"pair_grid_positions", "pair_grid_events"} <= tables
        row = conn.execute(
            "SELECT locked_profit, max_capital_at_risk, locked_profit_to_max_capital_at_risk FROM pair_grid_positions"
        ).fetchone()
        assert row[0] > 0
        assert row[1] > 0
        assert row[2] > 0
