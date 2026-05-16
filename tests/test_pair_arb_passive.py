from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_kalshibtc_1s_paper_executor import _snapshot_db

from kalshibtc.execution.pair_passive_inventory import PairArbPassiveConfig, PairArbPassiveManager
from kalshibtc.live_strategy_paper import run_live_strategy_paper_once
from kalshibtc.strategy.registry import create_strategy, strategy_names


def _iso(base: datetime, seconds: int) -> str:
    return (base + timedelta(seconds=seconds)).isoformat()


def _write_passive_snapshot_db(path: Path) -> None:
    base = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    close_time = datetime(2026, 5, 15, 12, 14, tzinfo=UTC)
    _snapshot_db(
        path,
        rows=[
            {
                "ts": _iso(base, 0),
                "market_ticker": "KXBTC15M-PASSIVE-A",
                "market_close_time": close_time.isoformat(),
                "btc_price": 100_000.0,
                "strike": 100_000.0,
                "btc_velocity_30s": 0.0,
                "yes_bid": 0.60,
                "yes_ask": 0.62,
                "no_bid": 0.41,
                "no_ask": 0.43,
            },
            {
                "ts": _iso(base, 30),
                "market_ticker": "KXBTC15M-PASSIVE-A",
                "market_close_time": close_time.isoformat(),
                "btc_price": 100_100.0,
                "strike": 100_000.0,
                "btc_velocity_30s": 2.0,
                "yes_bid": 0.69,
                "yes_ask": 0.71,
                "no_bid": 0.33,
                "no_ask": 0.35,
            },
            {
                "ts": _iso(base, 60),
                "market_ticker": "KXBTC15M-PASSIVE-A",
                "market_close_time": close_time.isoformat(),
                "btc_price": 99_900.0,
                "strike": 100_000.0,
                "btc_velocity_30s": -2.0,
                "yes_bid": 0.53,
                "yes_ask": 0.55,
                "no_bid": 0.48,
                "no_ask": 0.50,
            },
        ],
    )


def test_pair_arb_passive_is_registered() -> None:
    assert "pair_arb_passive" in strategy_names()
    assert create_strategy("pair_arb_passive").name == "pair_arb_passive"


def test_passive_places_bids_below_ask_and_fills_later_when_ask_touches_bid() -> None:
    manager = PairArbPassiveManager(PairArbPassiveConfig(grid_bid_discount=0.07, balanced_bid_discount=0.07, starter_bid_price=0.45))
    t0 = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)

    first = manager.on_book(
        event_key="passive-event",
        market_ticker="KXBTC15M-PASSIVE-A",
        ts=t0,
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=840.0,
        distance_from_strike=0.0,
        above_ask=0.62,
        below_ask=0.43,
    )
    assert first.orders_placed == 2
    assert first.fills == 0
    bids = {(order.side, order.bid_price) for order in manager.open_orders}
    assert ("ABOVE", 0.55) in bids
    assert ("BELOW", 0.36) in bids

    second = manager.on_book(
        event_key="passive-event",
        market_ticker="KXBTC15M-PASSIVE-A",
        ts=t0 + timedelta(seconds=30),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=810.0,
        distance_from_strike=100.0,
        above_ask=0.71,
        below_ask=0.35,
    )
    position = manager.positions["passive-event"]
    assert second.fills == 1
    assert position.held_below_qty == 5
    assert position.held_below_avg == 0.36
    assert position.held_above_qty == 0


def test_passive_only_places_bids_that_keep_projected_pair_cost_sane() -> None:
    manager = PairArbPassiveManager(PairArbPassiveConfig(starter_bid_price=0.60, max_bid_price=0.60))
    decision = manager.on_book(
        event_key="passive-event",
        market_ticker="KXBTC15M-PASSIVE-A",
        ts=datetime(2026, 5, 15, 12, 0, tzinfo=UTC),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=840.0,
        distance_from_strike=0.0,
        above_ask=0.70,
        below_ask=0.70,
    )
    assert decision.orders_placed == 0
    assert decision.reason == "projected_pair_cost_above_threshold"


def test_passive_respects_rebid_interval_order_age_target_and_imbalance() -> None:
    manager = PairArbPassiveManager(
        PairArbPassiveConfig(rebid_interval_seconds=60, max_order_age_seconds=60, max_imbalance_qty=5, target_pair_qty=5)
    )
    t0 = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    manager.on_book(
        event_key="passive-event",
        market_ticker="KXBTC15M-PASSIVE-A",
        ts=t0,
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=840.0,
        distance_from_strike=0.0,
        above_ask=0.50,
        below_ask=0.50,
    )
    early = manager.on_book(
        event_key="passive-event",
        market_ticker="KXBTC15M-PASSIVE-A",
        ts=t0 + timedelta(seconds=30),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=810.0,
        distance_from_strike=0.0,
        above_ask=0.50,
        below_ask=0.50,
    )
    assert early.orders_placed == 0
    assert early.reason == "rebid_interval"

    later = manager.on_book(
        event_key="passive-event",
        market_ticker="KXBTC15M-PASSIVE-A",
        ts=t0 + timedelta(seconds=61),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=779.0,
        distance_from_strike=0.0,
        above_ask=0.50,
        below_ask=0.50,
    )
    assert later.orders_placed == 0
    assert later.reason == "target_pair_qty_reached_or_order_exists"


def test_passive_rebalances_by_only_bidding_smaller_side_more_aggressively() -> None:
    manager = PairArbPassiveManager(
        PairArbPassiveConfig(
            rebid_interval_seconds=0,
            max_order_age_seconds=1,
            balanced_bid_discount=0.05,
            imbalance_bid_discount=0.02,
            max_imbalance_qty=25,
        )
    )
    t0 = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    manager.on_book(
        event_key="passive-event",
        market_ticker="KXBTC15M-PASSIVE-A",
        ts=t0,
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=840.0,
        distance_from_strike=0.0,
        above_ask=0.50,
        below_ask=0.50,
    )
    fill_above = manager.on_book(
        event_key="passive-event",
        market_ticker="KXBTC15M-PASSIVE-A",
        ts=t0 + timedelta(seconds=1),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=839.0,
        distance_from_strike=0.0,
        above_ask=0.45,
        below_ask=0.60,
    )
    assert fill_above.fills == 1
    assert manager.positions["passive-event"].held_above_qty == 5
    manager.open_orders.clear()

    rebalance = manager.on_book(
        event_key="passive-event",
        market_ticker="KXBTC15M-PASSIVE-A",
        ts=t0 + timedelta(seconds=61),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=779.0,
        distance_from_strike=0.0,
        above_ask=0.45,
        below_ask=0.50,
    )

    assert rebalance.orders_placed == 1
    assert rebalance.orders[0].side == "BELOW"
    assert rebalance.orders[0].bid_price == 0.48
    assert all(order.side == "BELOW" for order in manager.open_orders)


def test_passive_tracks_time_to_first_pair_largest_imbalance_and_streak_cap() -> None:
    manager = PairArbPassiveManager(
        PairArbPassiveConfig(
            rebid_interval_seconds=0,
            max_order_age_seconds=1,
            max_same_side_fills_without_pair=2,
            rebalance_only_when_imbalanced=False,
            max_imbalance_qty=25,
        )
    )
    t0 = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    for i in range(2):
        manager.open_orders.clear()
        manager.on_book(
            event_key="passive-event",
            market_ticker="KXBTC15M-PASSIVE-A",
            ts=t0 + timedelta(seconds=i * 10),
            market_close_time="2026-05-15T12:14:00+00:00",
            strike=100_000.0,
            seconds_to_close=840.0 - i * 10,
            distance_from_strike=0.0,
            above_ask=0.50,
            below_ask=0.50,
        )
        manager.on_book(
            event_key="passive-event",
            market_ticker="KXBTC15M-PASSIVE-A",
            ts=t0 + timedelta(seconds=i * 10 + 1),
            market_close_time="2026-05-15T12:14:00+00:00",
            strike=100_000.0,
            seconds_to_close=839.0 - i * 10,
            distance_from_strike=0.0,
            above_ask=0.45,
            below_ask=0.60,
        )

    position = manager.positions["passive-event"]
    assert position.held_above_qty == 10
    assert position.same_side_fill_streak == 2
    assert position.largest_inventory_imbalance == 10
    manager.open_orders.clear()

    capped = manager.on_book(
        event_key="passive-event",
        market_ticker="KXBTC15M-PASSIVE-A",
        ts=t0 + timedelta(seconds=30),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=810.0,
        distance_from_strike=0.0,
        above_ask=0.50,
        below_ask=0.90,
    )
    assert all(order.side != "ABOVE" for order in capped.orders)

    manager.open_orders.clear()
    manager.on_book(
        event_key="passive-event",
        market_ticker="KXBTC15M-PASSIVE-A",
        ts=t0 + timedelta(seconds=60),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=780.0,
        distance_from_strike=0.0,
        above_ask=0.90,
        below_ask=0.50,
    )
    paired = manager.on_book(
        event_key="passive-event",
        market_ticker="KXBTC15M-PASSIVE-A",
        ts=t0 + timedelta(seconds=75),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=765.0,
        distance_from_strike=0.0,
        above_ask=0.90,
        below_ask=0.45,
    )
    assert paired.fills == 1
    assert position.paired_qty == 5
    assert position.time_to_first_pair == 75
    assert position.same_side_fill_streak == 0


def test_live_pair_arb_passive_writes_orders_fills_positions_and_metrics(tmp_path: Path) -> None:
    snapshot_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_passive_snapshot_db(snapshot_db)

    summary = run_live_strategy_paper_once(
        snapshot_db=snapshot_db,
        runs_dir=runs_dir,
        strategies=["pair_arb_passive"],
        limit=10,
        no_official_settlement=True,
    )

    payload = summary.strategies[0]
    assert payload["strategy"] == "pair_arb_passive"
    assert payload["passive_orders"] >= 2
    assert payload["passive_fills"] >= 1
    assert payload["pair_positions"] == 1
    assert "locked_profit_to_max_capital_at_risk" in payload
    assert "time_to_first_pair" in payload
    assert "largest_inventory_imbalance" in payload
    assert "same_side_fill_streak" in payload

    results_db = runs_dir / "live" / "pair_arb_passive" / "results.sqlite3"
    with sqlite3.connect(results_db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"pair_passive_orders", "pair_passive_fills", "pair_passive_positions"} <= tables
        fill_count = conn.execute("SELECT COUNT(*) FROM pair_passive_fills").fetchone()[0]
        assert fill_count >= 1
        columns = {row[1] for row in conn.execute("PRAGMA table_info(pair_passive_positions)")}
        assert {"time_to_first_pair", "largest_inventory_imbalance", "same_side_fill_streak"} <= columns
