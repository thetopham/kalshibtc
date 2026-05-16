from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kalshibtc.config import RiskLimits
from kalshibtc.datafeed.models import OrderBookSnapshot, Tick
from kalshibtc.execution.risk import RiskManager
from kalshibtc.main import MarketStateBuilder
from kalshibtc.market.contract import ContractWindow
from kalshibtc.replay.cli import main as replay_main
from kalshibtc.strategy.registry import create_strategy, strategy_names
from kalshibtc.strategy.volatility_features import VolatilityFeatureBuilder
from kalshibtc.strategy.volatility_inventory import (
    VolatilityInventoryConfig,
    VolatilityInventoryStrategy,
)


def _contract() -> ContractWindow:
    return ContractWindow(
        ticker="KXBTC15M-VOL",
        strike=100_000.0,
        open_time=datetime(2026, 5, 15, 12, 0, tzinfo=UTC),
        close_time=datetime(2026, 5, 15, 12, 15, tzinfo=UTC),
    )


def _state(price: float, seconds: int, *, yes_ask: float = 0.55, no_ask: float = 0.45):
    ts = datetime(2026, 5, 15, 12, 0, tzinfo=UTC) + timedelta(seconds=seconds)
    tick = Tick(ts=ts, price=price, source="test", symbol="BTC-USD")
    book = OrderBookSnapshot(
        ts=ts,
        market_ticker="KXBTC15M-VOL",
        yes_bid=max(0.01, yes_ask - 0.02),
        yes_ask=yes_ask,
        no_bid=max(0.01, no_ask - 0.02),
        no_ask=no_ask,
    )
    return MarketStateBuilder(contract=_contract()).from_tick_and_book(
        tick=tick,
        orderbook=book,
        slope_30s=None,
    )


def test_feature_builder_detects_velocity_away_and_expansion_regime() -> None:
    builder = VolatilityFeatureBuilder(atr_window_seconds=60.0, baseline_window=4)
    builder.update(_state(100_000.0, 0))
    builder.update(_state(99_980.0, 10))
    builder.update(_state(99_930.0, 20))
    features = builder.update(_state(99_820.0, 30))

    assert features.distance_from_strike == pytest.approx(-180.0)
    assert features.distance_from_strike_abs == pytest.approx(180.0)
    assert features.velocity_away_from_strike > 0
    assert features.atr_1m > 0
    assert features.expansion_regime is True
    assert features.stabilization_regime is False


def test_expansion_regime_causes_momentum_side_add() -> None:
    strategy = VolatilityInventoryStrategy(
        VolatilityInventoryConfig(
            min_atr_1m=5.0,
            expansion_velocity_threshold=2.0,
            base_notional=10.0,
            max_notional_per_add=200.0,
        )
    )
    for state in [_state(100_000.0, 0), _state(99_980.0, 10), _state(99_930.0, 20)]:
        strategy.on_tick(state)

    signal = strategy.on_tick(_state(99_820.0, 30, yes_ask=0.22, no_ask=0.78))

    assert signal.side == "long_below"
    assert signal.reason == "expansion momentum pyramid"
    assert signal.target_notional > 10.0
    assert signal.features["expansion_regime"] is True


def test_stabilization_after_expansion_causes_crushed_side_add() -> None:
    strategy = VolatilityInventoryStrategy(
        VolatilityInventoryConfig(
            min_atr_1m=5.0,
            expansion_velocity_threshold=2.0,
            slowdown_threshold=1.0,
            base_notional=10.0,
            max_notional_per_add=200.0,
        )
    )
    for state in [
        _state(100_000.0, 0),
        _state(99_970.0, 10),
        _state(99_880.0, 20, yes_ask=0.30, no_ask=0.70),
        _state(99_780.0, 30, yes_ask=0.20, no_ask=0.82),
    ]:
        strategy.on_tick(state)

    signal = strategy.on_tick(_state(99_790.0, 40, yes_ask=0.18, no_ask=0.84))

    assert signal.side == "long_above"
    assert signal.reason == "stabilization crushed-side accumulation"
    assert signal.features["stabilization_regime"] is True
    assert signal.target_notional > 0


def test_low_atr_flat_market_produces_no_or_tiny_add() -> None:
    strategy = VolatilityInventoryStrategy(VolatilityInventoryConfig(min_atr_1m=5.0, base_notional=10.0))
    for state in [_state(100_000.0, 0), _state(100_001.0, 10), _state(99_999.0, 20)]:
        signal = strategy.on_tick(state)

    assert signal.side == "none"
    assert signal.target_notional == 0.0
    assert "flat/low volatility" in signal.reason


def test_sizing_uses_notional_divided_by_price_not_fixed_shares() -> None:
    strategy = VolatilityInventoryStrategy(
        VolatilityInventoryConfig(
            min_atr_1m=5.0,
            expansion_velocity_threshold=2.0,
            base_notional=20.0,
            floor_price=0.05,
            max_notional_per_add=200.0,
        )
    )
    for state in [_state(100_000.0, 0), _state(99_970.0, 10), _state(99_900.0, 20)]:
        strategy.on_tick(state)
    state = _state(99_760.0, 30, yes_ask=0.20, no_ask=0.80)
    signal = strategy.on_tick(state)

    assert signal.target_notional > 20.0
    assert signal.estimated_shares == pytest.approx(signal.target_notional / 0.80)

    decision = RiskManager(RiskLimits(base_size_dollars=1.0, max_position_dollars=500.0)).evaluate(state, signal)
    assert decision.allowed is True
    assert decision.size_dollars == pytest.approx(signal.target_notional)


def test_volatility_inventory_module_does_not_import_live_order_adapter() -> None:
    source = Path("src/kalshibtc/strategy/volatility_inventory.py").read_text()

    assert "execution.kalshi" not in source
    assert "KalshiBrokerAdapter" not in source


def test_registry_exposes_volatility_inventory_without_live_order_imports() -> None:
    assert "volatility_inventory" in strategy_names()
    strategy = create_strategy("volatility_inventory")

    assert strategy.name == "volatility_inventory"
    assert type(strategy).__module__ == "kalshibtc.strategy.volatility_inventory"


def _write_feed_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    open_time = datetime(2026, 5, 15, 12, 0, tzinfo=UTC).isoformat()
    close_time = datetime(2026, 5, 15, 12, 15, tzinfo=UTC).isoformat()
    rows = [
        (0, 100_000.0, 0.48, 0.52, 0.48, 0.52),
        (10, 99_970.0, 0.32, 0.34, 0.66, 0.68),
        (20, 99_880.0, 0.24, 0.26, 0.74, 0.76),
        (30, 99_760.0, 0.18, 0.20, 0.80, 0.82),
        (40, 99_750.0, 0.16, 0.18, 0.82, 0.84),
    ]
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE realtime_snapshots_1s (
                ts TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                market_open_time TEXT,
                market_close_time TEXT NOT NULL,
                btc_price REAL NOT NULL,
                strike REAL NOT NULL,
                yes_bid REAL,
                yes_ask REAL,
                no_bid REAL,
                no_ask REAL,
                orderbook_sequence INTEGER,
                raw_json TEXT,
                PRIMARY KEY (market_ticker, ts)
            )
            """
        )
        for index, (second, price, yes_bid, yes_ask, no_bid, no_ask) in enumerate(rows, start=1):
            ts = (datetime(2026, 5, 15, 12, 0, tzinfo=UTC) + timedelta(seconds=second)).isoformat()
            conn.execute(
                """
                INSERT INTO realtime_snapshots_1s (
                    ts, market_ticker, market_open_time, market_close_time, btc_price,
                    strike, yes_bid, yes_ask, no_bid, no_ask, orderbook_sequence, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    "KXBTC15M-VOL",
                    open_time,
                    close_time,
                    price,
                    100_000.0,
                    yes_bid,
                    yes_ask,
                    no_bid,
                    no_ask,
                    index,
                    json.dumps({"strike": 100_000.0, "market_close_time": close_time, "market_open_time": open_time}),
                ),
            )


def test_replay_cli_writes_volatility_inventory_research_tables(tmp_path: Path) -> None:
    feed_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_feed_db(feed_db)

    exit_code = replay_main(
        [
            "--feed-db",
            str(feed_db),
            "--runs-dir",
            str(runs_dir),
            "--strategy",
            "volatility_inventory",
            "--run-id",
            "unit-vol",
            "--max-open-positions",
            "10",
            "--max-position-dollars",
            "500",
            "--json",
        ]
    )

    assert exit_code == 0
    run_dir = runs_dir / "volatility_inventory" / "unit-vol"
    metrics = json.loads((run_dir / "metrics.json").read_text())
    assert metrics["strategy"] == "volatility_inventory"
    assert metrics["research_metrics"]["volatility_feature_rows"] >= 1
    assert metrics["research_metrics"]["inventory_decisions"] >= 1

    with sqlite3.connect(run_dir / "results.sqlite3") as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        feature_rows = conn.execute("SELECT COUNT(*) FROM volatility_features").fetchone()[0]
        decisions = conn.execute("SELECT COUNT(*) FROM inventory_decisions WHERE target_notional > 0").fetchone()[0]
        positions = conn.execute("SELECT COUNT(*) FROM inventory_positions").fetchone()[0]
        equity = conn.execute("SELECT COUNT(*) FROM inventory_equity_curve").fetchone()[0]

    assert {"volatility_features", "inventory_decisions", "inventory_positions", "inventory_equity_curve"} <= tables
    assert feature_rows >= 1
    assert decisions >= 1
    assert positions >= 1
    assert equity >= 1
