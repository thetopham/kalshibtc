from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from test_kalshibtc_1s_paper_executor import _iso, _snapshot_db

from kalshibtc.execution.inventory_vol_regime import InventoryVolRegimeConfig, InventoryVolRegimeManager
from kalshibtc import dashboard
from kalshibtc.live_strategy_paper import run_live_strategy_paper_once
from kalshibtc.strategy.registry import create_strategy, strategy_names


def _write_inventory_vol_regime_db(path: Path) -> None:
    close_time = datetime(2026, 5, 15, 12, 14, tzinfo=UTC)
    _snapshot_db(
        path,
        rows=[
            {
                "ts": _iso(10),
                "market_ticker": "KXBTC15M-IVREG-A",
                "market_close_time": close_time.isoformat(),
                "btc_price": 100_000.0,
                "strike": 100_000.0,
                "btc_velocity_30s": 0.0,
                "seconds_to_close": 830.0,
                "yes_bid": 0.49,
                "yes_ask": 0.52,
                "no_bid": 0.47,
                "no_ask": 0.56,
            },
            {
                "ts": _iso(20),
                "market_ticker": "KXBTC15M-IVREG-A",
                "market_close_time": close_time.isoformat(),
                "btc_price": 99_700.0,
                "strike": 100_000.0,
                "btc_velocity_30s": -9.0,
                "seconds_to_close": 820.0,
                "yes_bid": 0.26,
                "yes_ask": 0.28,
                "no_bid": 0.73,
                "no_ask": 0.78,
            },
            {
                "ts": _iso(40),
                "market_ticker": "KXBTC15M-IVREG-A",
                "market_close_time": close_time.isoformat(),
                "btc_price": 99_610.0,
                "strike": 100_000.0,
                "btc_velocity_30s": -2.0,
                "seconds_to_close": 260.0,
                "yes_bid": 0.18,
                "yes_ask": 0.20,
                "no_bid": 0.80,
                "no_ask": 0.84,
            },
        ],
    )


def test_inventory_vol_regime_is_registered() -> None:
    assert "inventory_vol_regime" in strategy_names()
    assert create_strategy("inventory_vol_regime").name == "inventory_vol_regime"


def test_inventory_vol_regime_dynamic_sizing_expands_with_atr_distance_and_velocity() -> None:
    manager = InventoryVolRegimeManager(
        InventoryVolRegimeConfig(
            base_notional=10,
            max_notional_per_add=500,
            cooldown_seconds=0,
            slippage=0.0,
            fee_per_contract=0.0,
        )
    )
    base = datetime(2026, 5, 15, 12, 0, 10, tzinfo=UTC)
    first = manager.on_book(
        event_key="ivreg-event",
        market_ticker="KXBTC15M-IVREG-A",
        ts=base,
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=830.0,
        distance_from_strike=20.0,
        btc_price=100_020.0,
        btc_velocity_30s=1.0,
        above_bid=0.50,
        above_ask=0.52,
        below_bid=0.46,
        below_ask=0.54,
        atr_1m=10.0,
        atr_expansion_rate=0.8,
        macd_histogram=1.0,
        macd_slope=0.1,
    )
    second = manager.on_book(
        event_key="ivreg-event",
        market_ticker="KXBTC15M-IVREG-A",
        ts=base.replace(second=20),
        market_close_time="2026-05-15T12:14:00+00:00",
        strike=100_000.0,
        seconds_to_close=820.0,
        distance_from_strike=-300.0,
        btc_price=99_700.0,
        btc_velocity_30s=-9.0,
        above_bid=0.25,
        above_ask=0.28,
        below_bid=0.72,
        below_ask=0.78,
        atr_1m=70.0,
        atr_expansion_rate=3.0,
        macd_histogram=-9.0,
        macd_slope=-4.0,
    )

    position = manager.positions["ivreg-event"]
    assert first.reason == "starter_countertrend_leg_2x"
    assert first.fills == 2
    assert first.fill_events[0].reason == "starter_trend_leg_3x"
    assert first.fill_events[1].reason == "starter_countertrend_leg_2x"
    assert first.fill_events[0].notional > first.fill_events[1].notional
    assert second.reason == "momentum_expansion_pyramid"
    assert second.fill_events[0].quantity > first.fill_events[0].quantity
    assert position.held_below_qty > position.held_above_qty
    assert second.features is not None
    assert second.features.expansion_regime is True
    assert second.features.volatility_regime_score > first.features.volatility_regime_score  # type: ignore[union-attr]


def test_inventory_vol_regime_stabilization_and_late_compression_accumulates_crushed_side() -> None:
    manager = InventoryVolRegimeManager(InventoryVolRegimeConfig(cooldown_seconds=0, slippage=0.0, fee_per_contract=0.0))
    base = datetime(2026, 5, 15, 12, 0, 10, tzinfo=UTC)
    manager.on_book(event_key="ivreg-event", market_ticker="KXBTC15M-IVREG-A", ts=base, market_close_time="2026-05-15T12:14:00+00:00", strike=100_000, seconds_to_close=820, distance_from_strike=-300, btc_price=99_700, btc_velocity_30s=-9, above_bid=0.25, above_ask=0.28, below_bid=0.72, below_ask=0.78, atr_1m=70, atr_expansion_rate=3.0, macd_histogram=-9, macd_slope=-4)
    stabilize = manager.on_book(event_key="ivreg-event", market_ticker="KXBTC15M-IVREG-A", ts=base.replace(second=30), market_close_time="2026-05-15T12:14:00+00:00", strike=100_000, seconds_to_close=780, distance_from_strike=-380, btc_price=99_620, btc_velocity_30s=-2, above_bid=0.18, above_ask=0.20, below_bid=0.80, below_ask=0.84, atr_1m=75, atr_expansion_rate=3.1, macd_histogram=-2, macd_slope=7)
    compress = manager.on_book(event_key="ivreg-event", market_ticker="KXBTC15M-IVREG-A", ts=base.replace(second=50), market_close_time="2026-05-15T12:14:00+00:00", strike=100_000, seconds_to_close=250, distance_from_strike=-420, btc_price=99_580, btc_velocity_30s=-1, above_bid=0.17, above_ask=0.19, below_bid=0.81, below_ask=0.84, atr_1m=65, atr_expansion_rate=2.7, macd_histogram=-1, macd_slope=1)

    position = manager.positions["ivreg-event"]
    assert stabilize.reason == "post_expansion_stabilization_crushed_side"
    assert stabilize.fill_events[0].side == "ABOVE"
    assert compress.reason == "late_compression_crushed_side"
    assert compress.fill_events[0].side == "ABOVE"
    assert position.held_above_qty > 0
    metrics = manager.research_metrics()
    assert metrics["compression_after_directional_expansion"]
    assert metrics["mark_to_market_equity_curve"]
    assert metrics["inventory_imbalance_heatmap"]


def test_live_inventory_vol_regime_writes_paper_only_tables_and_metrics(tmp_path: Path) -> None:
    snapshot_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_inventory_vol_regime_db(snapshot_db)

    summary = run_live_strategy_paper_once(
        snapshot_db=snapshot_db,
        runs_dir=runs_dir,
        strategies=["inventory_vol_regime"],
        limit=10,
        no_official_settlement=True,
    )

    payload = summary.strategies[0]
    assert payload["strategy"] == "inventory_vol_regime"
    assert payload["mode"] == "live_paper"
    assert payload["inventory_positions"] == 1
    assert payload["fills"] >= 3
    assert "inventory_additions_vs_atr_spikes" in payload["research_metrics"]
    assert "distance_from_strike_velocity_graph" in payload["research_metrics"]

    results_db = runs_dir / "live" / "inventory_vol_regime" / "results.sqlite3"
    with sqlite3.connect(results_db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"inventory_vol_regime_positions", "inventory_vol_regime_events", "inventory_vol_regime_research_metrics"} <= tables
        row = conn.execute("SELECT held_above_qty, held_below_qty, total_cost, mark_to_market_equity FROM inventory_vol_regime_positions").fetchone()
        assert row[0] > 0 or row[1] > 0
        assert row[2] > 0
        assert conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 0

    detail = dashboard.collect_strategy_run_detail_data(runs_dir=runs_dir, strategy="inventory_vol_regime", run_id="live")
    assert detail["inventory_vol_regime_positions"]
    assert detail["inventory_vol_regime_events"]
    assert detail["inventory_vol_regime_research_metrics"]
    html = dashboard.render_strategy_run_detail_html(detail)
    assert "Volatility regime state" in html
    assert "ATR expansion graph" in html
    assert "Distance-from-strike velocity graph" in html
    assert "Inventory imbalance heatmap" in html
    assert "Per-side inventory ladder" in html
