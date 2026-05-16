from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from test_live_strategy_paper import _iso
from test_kalshibtc_1s_paper_executor import _snapshot_db

from kalshibtc import dashboard
from kalshibtc.execution.pair_inventory import HedgeManager, InventoryManager, PairArbConfig
from kalshibtc.live_strategy_paper import run_live_strategy_paper_once
from kalshibtc.strategy.registry import create_strategy, strategy_names


def _write_pair_snapshot_db(path: Path) -> None:
    close_time = datetime(2026, 5, 15, 12, 14, tzinfo=UTC)
    _snapshot_db(
        path,
        rows=[
            {
                "ts": _iso(10),
                "market_ticker": "KXBTC15M-PAIR-A",
                "market_close_time": close_time.isoformat(),
                "btc_price": 100_080.0,
                "strike": 100_000.0,
                "btc_velocity_30s": 3.0,
                "yes_bid": 0.53,
                "yes_ask": 0.55,
                "no_bid": 0.43,
                "no_ask": 0.45,
            },
            {
                "ts": _iso(20),
                "market_ticker": "KXBTC15M-PAIR-A",
                "market_close_time": close_time.isoformat(),
                "btc_price": 100_180.0,
                "strike": 100_000.0,
                "btc_velocity_30s": 2.0,
                "yes_bid": 0.82,
                "yes_ask": 0.84,
                "no_bid": 0.13,
                "no_ask": 0.15,
            },
        ],
    )


def _write_rejected_seed_snapshot_db(path: Path) -> None:
    close_time = datetime(2026, 5, 15, 12, 14, tzinfo=UTC)
    _snapshot_db(
        path,
        rows=[
            {
                "ts": _iso(10),
                "market_ticker": "KXBTC15M-PAIR-HIGH",
                "market_close_time": close_time.isoformat(),
                "btc_price": 100_080.0,
                "strike": 100_000.0,
                "btc_velocity_30s": 3.0,
                "yes_bid": 0.89,
                "yes_ask": 0.91,
                "no_bid": 0.07,
                "no_ask": 0.09,
            },
            {
                "ts": _iso(20),
                "market_ticker": "KXBTC15M-PAIR-OK",
                "market_close_time": close_time.isoformat(),
                "btc_price": 100_080.0,
                "strike": 100_000.0,
                "btc_velocity_30s": 3.0,
                "yes_bid": 0.58,
                "yes_ask": 0.60,
                "no_bid": 0.36,
                "no_ask": 0.38,
            },
        ],
    )


def test_pair_arb_strategy_is_registered_as_primary_pair_strategy() -> None:
    assert "pair_arb" in strategy_names()
    strategy = create_strategy("pair_arb")
    assert strategy.name == "pair_arb"


def test_inventory_and_hedge_manager_create_same_market_pair_below_threshold() -> None:
    inventory = InventoryManager(config=PairArbConfig(pair_cost_threshold=0.95, slippage=0.0, max_unpaired_qty=100))
    first = inventory.buy_initial_leg(
        event_key="KXBTC15M-PAIR-A|2026-05-15T12:14:00+00:00|100000.0",
        market_ticker="KXBTC15M-PAIR-A",
        side="ABOVE",
        ask_price=0.55,
        quantity=100,
        ts=datetime(2026, 5, 15, 12, 0, 10, tzinfo=UTC),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        distance_from_strike=80.0,
        seconds_to_close=830.0,
        opposite_ask=0.45,
    )
    assert first.filled is True

    hedge = HedgeManager(config=PairArbConfig(pair_cost_threshold=0.95, slippage=0.0, max_unpaired_qty=100)).evaluate_and_fill(
        inventory=inventory,
        event_key="KXBTC15M-PAIR-A|2026-05-15T12:14:00+00:00|100000.0",
        opposite_ask=0.15,
        ts=datetime(2026, 5, 15, 12, 0, 20, tzinfo=UTC),
        distance_from_strike=180.0,
        seconds_to_close=820.0,
    )

    position = inventory.positions["KXBTC15M-PAIR-A|2026-05-15T12:14:00+00:00|100000.0"]
    assert hedge.filled is True
    assert position.held_above_qty == 100
    assert position.held_below_qty == 25
    assert position.held_above_avg == 0.55
    assert position.held_below_avg == 0.15
    assert position.pair_cost == 0.70
    assert position.locked_profit == 7.5
    assert position.unpaired_directional_exposure == 75


def test_hedge_manager_logs_missed_opportunity_reason_when_pair_cost_too_high() -> None:
    inventory = InventoryManager(config=PairArbConfig(pair_cost_threshold=0.95, slippage=0.0, max_unpaired_qty=100))
    inventory.buy_initial_leg(
        event_key="same-event",
        market_ticker="KXBTC15M-PAIR-A",
        side="ABOVE",
        ask_price=0.55,
        quantity=100,
        ts=datetime(2026, 5, 15, 12, 0, 10, tzinfo=UTC),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        distance_from_strike=80.0,
        seconds_to_close=830.0,
        opposite_ask=0.45,
    )

    result = HedgeManager(config=PairArbConfig(pair_cost_threshold=0.95, slippage=0.0, max_unpaired_qty=100)).evaluate_and_fill(
        inventory=inventory,
        event_key="same-event",
        opposite_ask=0.43,
        ts=datetime(2026, 5, 15, 12, 0, 20, tzinfo=UTC),
        distance_from_strike=90.0,
        seconds_to_close=820.0,
    )

    assert result.filled is False
    assert result.reason == "pair_cost_above_threshold"
    assert inventory.missed_hedges[-1].reason == "pair_cost_above_threshold"


def test_seed_price_cap_rejects_91c_and_allows_60c_seed() -> None:
    inventory = InventoryManager(config=PairArbConfig(slippage=0.0))
    rejected = inventory.buy_initial_leg(
        event_key="high-seed",
        market_ticker="KXBTC15M-HIGH",
        side="ABOVE",
        ask_price=0.91,
        quantity=25,
        ts=datetime(2026, 5, 15, 12, 0, 10, tzinfo=UTC),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        distance_from_strike=80.0,
        seconds_to_close=830.0,
        opposite_ask=0.09,
    )
    allowed = inventory.buy_initial_leg(
        event_key="ok-seed",
        market_ticker="KXBTC15M-OK",
        side="ABOVE",
        ask_price=0.60,
        quantity=25,
        ts=datetime(2026, 5, 15, 12, 0, 20, tzinfo=UTC),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        distance_from_strike=80.0,
        seconds_to_close=820.0,
        opposite_ask=0.38,
    )

    assert rejected.filled is False
    assert rejected.reason == "seed_price_too_high"
    assert allowed.filled is True
    assert inventory.rejected_seed_attempts[-1].reason == "seed_price_too_high"


def test_seed_unpaired_exposure_is_capped_and_opposite_room_required() -> None:
    inventory = InventoryManager(config=PairArbConfig(slippage=0.0, max_seed_price=0.80, max_unpaired_qty=25, min_expected_opposite_room=0.20))
    too_large = inventory.buy_initial_leg(
        event_key="too-large",
        market_ticker="KXBTC15M-LARGE",
        side="ABOVE",
        ask_price=0.60,
        quantity=100,
        ts=datetime(2026, 5, 15, 12, 0, 10, tzinfo=UTC),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        distance_from_strike=80.0,
        seconds_to_close=830.0,
        opposite_ask=0.20,
    )
    no_room = inventory.buy_initial_leg(
        event_key="no-room",
        market_ticker="KXBTC15M-NOROOM",
        side="ABOVE",
        ask_price=0.76,
        quantity=25,
        ts=datetime(2026, 5, 15, 12, 0, 20, tzinfo=UTC),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        distance_from_strike=80.0,
        seconds_to_close=820.0,
        opposite_ask=0.35,
    )

    assert too_large.reason == "max_unpaired_qty_reached"
    assert no_room.reason == "insufficient_opposite_room"
    assert [attempt.reason for attempt in inventory.rejected_seed_attempts] == [
        "max_unpaired_qty_reached",
        "insufficient_opposite_room",
    ]


def test_live_pair_arb_paper_writes_pair_tables_and_dashboard_fields(tmp_path: Path) -> None:
    snapshot_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_pair_snapshot_db(snapshot_db)

    summary = run_live_strategy_paper_once(
        snapshot_db=snapshot_db,
        runs_dir=runs_dir,
        strategies=["pair_arb"],
        limit=10,
        no_official_settlement=True,
    )

    pair_summary = summary.strategies[0]
    assert pair_summary["strategy"] == "pair_arb"
    assert pair_summary["pair_positions"] == 1
    assert pair_summary["hedge_events"] >= 1
    assert pair_summary["missed_hedge_events"] >= 0
    assert pair_summary["locked_profit"] == 7.0
    assert pair_summary["unpaired_directional_exposure"] == 0

    results_db = runs_dir / "live" / "pair_arb" / "results.sqlite3"
    with sqlite3.connect(results_db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"pair_positions", "hedge_events", "missed_hedge_opportunities", "rejected_seed_attempts"} <= tables
        row = conn.execute(
            "SELECT held_above_qty, held_above_avg, held_below_qty, held_below_avg, pair_cost, locked_profit, unpaired_directional_exposure FROM pair_positions"
        ).fetchone()
        assert row == (25.0, 0.56, 25.0, 0.16, 0.72, 7.0, 0.0)

    detail = dashboard.collect_strategy_run_detail_data(runs_dir=runs_dir, strategy="live", run_id="pair_arb")
    html = dashboard.render_strategy_run_detail_html(detail)
    assert "Pair positions" in html
    assert "held above qty" in html
    assert "held below qty" in html
    assert "pair cost" in html
    assert "locked profit" in html
    assert "unpaired directional exposure" in html


def test_rejected_seed_reasons_appear_in_sqlite_and_dashboard(tmp_path: Path) -> None:
    snapshot_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_rejected_seed_snapshot_db(snapshot_db)

    summary = run_live_strategy_paper_once(
        snapshot_db=snapshot_db,
        runs_dir=runs_dir,
        strategies=["pair_arb"],
        limit=10,
        no_official_settlement=True,
    )

    assert summary.strategies[0]["pair_positions"] == 1
    assert summary.strategies[0]["rejected_seed_attempts"] == 1
    results_db = runs_dir / "live" / "pair_arb" / "results.sqlite3"
    with sqlite3.connect(results_db) as conn:
        reasons = [row[0] for row in conn.execute("SELECT reason FROM rejected_seed_attempts ORDER BY id")]
        assert reasons == ["seed_price_too_high"]

    detail = dashboard.collect_strategy_run_detail_data(runs_dir=runs_dir, strategy="live", run_id="pair_arb")
    html = dashboard.render_strategy_run_detail_html(detail)
    assert "Rejected seed attempts" in html
    assert "seed_price_too_high" in html
