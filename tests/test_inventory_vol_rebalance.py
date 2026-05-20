from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_kalshibtc_1s_paper_executor import _iso, _snapshot_db

from kalshibtc.execution.inventory_vol_rebalance import InventoryVolRebalanceConfig, InventoryVolRebalanceManager
from kalshibtc.live_strategy_paper import run_live_strategy_paper_once
from kalshibtc import dashboard
from kalshibtc.strategy.registry import create_strategy, strategy_names


def _write_inventory_vol_db(path: Path) -> None:
    close_time = datetime(2026, 5, 15, 12, 14, tzinfo=UTC)
    _snapshot_db(
        path,
        rows=[
            {
                "ts": _iso(10),
                "market_ticker": "KXBTC15M-IVR-A",
                "market_close_time": close_time.isoformat(),
                "btc_price": 100_000.0,
                "strike": 100_000.0,
                "btc_velocity_30s": 0.0,
                "seconds_to_close": 830.0,
                "yes_bid": 0.49,
                "yes_ask": 0.51,
                "no_bid": 0.48,
                "no_ask": 0.50,
            },
            {
                "ts": _iso(20),
                "market_ticker": "KXBTC15M-IVR-A",
                "market_close_time": close_time.isoformat(),
                "btc_price": 100_260.0,
                "strike": 100_000.0,
                "btc_velocity_30s": 7.0,
                "seconds_to_close": 820.0,
                "yes_bid": 0.80,
                "yes_ask": 0.82,
                "no_bid": 0.17,
                "no_ask": 0.19,
            },
            {
                "ts": _iso(31),
                "market_ticker": "KXBTC15M-IVR-A",
                "market_close_time": close_time.isoformat(),
                "btc_price": 100_090.0,
                "strike": 100_000.0,
                "btc_velocity_30s": -3.0,
                "seconds_to_close": 809.0,
                "yes_bid": 0.61,
                "yes_ask": 0.64,
                "no_bid": 0.34,
                "no_ask": 0.37,
            },
        ],
    )


def test_inventory_vol_rebalance_is_registered() -> None:
    assert "inventory_vol_rebalance" in strategy_names()
    assert create_strategy("inventory_vol_rebalance").name == "inventory_vol_rebalance"


def test_inventory_vol_rebalance_starts_both_sides_then_adds_crushed_side_without_pair_completion() -> None:
    manager = InventoryVolRebalanceManager(
        InventoryVolRebalanceConfig(
            starter_qty_per_side=5,
            add_qty=5,
            cheap_add_threshold=0.35,
            deep_cheap_threshold=0.20,
            max_inventory_ratio=3.0,
            post_impulse_cooldown_seconds=10,
            volatility_add_multiplier=True,
            fee_per_contract=0.0,
            slippage=0.0,
        )
    )
    ts = datetime(2026, 5, 15, 12, 0, 10, tzinfo=UTC)

    starter = manager.on_book(
        event_key="ivr-event",
        market_ticker="KXBTC15M-IVR-A",
        ts=ts,
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=830.0,
        distance_from_strike=0.0,
        btc_price=100_000.0,
        btc_velocity_30s=0.0,
        above_bid=0.49,
        above_ask=0.51,
        below_bid=0.48,
        below_ask=0.50,
    )
    assert starter.reason == "starter_inventory_both_sides"
    assert starter.fills == 2

    add = manager.on_book(
        event_key="ivr-event",
        market_ticker="KXBTC15M-IVR-A",
        ts=ts.replace(second=20),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=820.0,
        distance_from_strike=260.0,
        btc_price=100_260.0,
        btc_velocity_30s=7.0,
        above_bid=0.80,
        above_ask=0.82,
        below_bid=0.17,
        below_ask=0.19,
    )

    position = manager.positions["ivr-event"]
    assert add.reason == "volatility_add_crushed_side"
    assert add.fills == 1
    assert position.held_above_qty == 5
    assert position.held_below_qty == 15
    assert position.inventory_imbalance_ratio == pytest.approx(3.0)
    assert position.blended_basis == pytest.approx(position.held_above_avg + position.held_below_avg)
    assert position.mark_to_market_equity(above_bid=0.80, below_bid=0.17) > -1.0
    assert position.unrealized_pnl(above_bid=0.80, below_bid=0.17) is not None
    assert position.max_drawdown <= 0.0


def test_inventory_vol_rebalance_reduces_rebounded_overweight_inventory_and_records_research_metrics() -> None:
    manager = InventoryVolRebalanceManager(InventoryVolRebalanceConfig(slippage=0.0, fee_per_contract=0.0))
    base = datetime(2026, 5, 15, 12, 0, 10, tzinfo=UTC)
    manager.on_book(event_key="ivr-event", market_ticker="KXBTC15M-IVR-A", ts=base, market_close_time="2026-05-15T12:14:00+00:00", strike=100_000, seconds_to_close=830, distance_from_strike=0, btc_price=100_000, btc_velocity_30s=0, above_bid=0.49, above_ask=0.51, below_bid=0.48, below_ask=0.50)
    manager.on_book(event_key="ivr-event", market_ticker="KXBTC15M-IVR-A", ts=base.replace(second=20), market_close_time="2026-05-15T12:14:00+00:00", strike=100_000, seconds_to_close=820, distance_from_strike=260, btc_price=100_260, btc_velocity_30s=7, above_bid=0.80, above_ask=0.82, below_bid=0.17, below_ask=0.19)
    reduce = manager.on_book(event_key="ivr-event", market_ticker="KXBTC15M-IVR-A", ts=base.replace(second=31), market_close_time="2026-05-15T12:14:00+00:00", strike=100_000, seconds_to_close=809, distance_from_strike=90, btc_price=100_090, btc_velocity_30s=-3, above_bid=0.61, above_ask=0.64, below_bid=0.34, below_ask=0.37)

    position = manager.positions["ivr-event"]
    assert reduce.reason == "reduce_rebounded_overweight_inventory"
    assert reduce.reductions == 1
    assert position.realized_pnl > 0
    assert position.held_below_qty == 10
    assert manager.research_metrics()["add_events_vs_btc_volatility_spikes"][0]["btc_velocity_30s"] == 7
    assert manager.research_metrics()["time_to_reversion_after_add"][0]["seconds_to_reversion"] == 11
    assert manager.research_metrics()["realized_volatility_harvested_per_cycle"] > 0


def test_live_inventory_vol_rebalance_writes_paper_only_tables_and_dashboard_panels(tmp_path: Path) -> None:
    snapshot_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_inventory_vol_db(snapshot_db)

    summary = run_live_strategy_paper_once(
        snapshot_db=snapshot_db,
        runs_dir=runs_dir,
        strategies=["inventory_vol_rebalance"],
        limit=10,
        no_official_settlement=True,
    )

    payload = summary.strategies[0]
    assert payload["strategy"] == "inventory_vol_rebalance"
    assert payload["mode"] == "live_paper"
    assert payload["inventory_positions"] == 1
    assert payload["fills"] >= 3
    assert payload["reductions"] >= 1
    assert payload["realized_pnl"] > 0
    assert "add_events_vs_btc_volatility_spikes" in payload["research_metrics"]

    results_db = runs_dir / "live" / "inventory_vol_rebalance" / "results.sqlite3"
    with sqlite3.connect(results_db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"inventory_vol_positions", "inventory_vol_events", "inventory_vol_research_metrics"} <= tables
        row = conn.execute("SELECT held_above_qty, held_below_qty, blended_basis, realized_pnl, inventory_imbalance_ratio, max_drawdown FROM inventory_vol_positions").fetchone()
        assert row[0] > 0 and row[1] > 0
        assert row[2] > 0
        assert row[3] > 0
        assert row[4] <= 3.0

    detail = dashboard.collect_strategy_run_detail_data(runs_dir=runs_dir, strategy="inventory_vol_rebalance", run_id="live")
    assert detail["inventory_vol_positions"][0]["inventory_imbalance_ratio"] <= 3.0
    html = dashboard.render_strategy_run_detail_html(detail)
    assert "Inventory imbalance gauge" in html
    assert "Blended basis over time" in html
    assert "Mark-to-market equity curve" in html
    assert "Add/reduction event timeline" in html
    assert "Volatility overlay" in html
    assert "Per-side inventory ladder" in html
    assert "No order submission" in html
