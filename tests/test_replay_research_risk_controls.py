from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_feed_replay_architecture import _write_feed_db

from kalshibtc.backtest.metrics import compute_metrics
from kalshibtc.replay.cli import main as replay_main
from kalshibtc.replay.settlement import compute_portfolio_settlement, estimate_replay_fill_pnls


def test_replay_cli_applies_realistic_defaults_to_cross_venue_strategy_runs(tmp_path: Path) -> None:
    feed_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_feed_db(feed_db)

    assert replay_main(
        [
            "--feed-db",
            str(feed_db),
            "--runs-dir",
            str(runs_dir),
            "--strategy",
            "simple_directional",
            "--run-id",
            "improved-defaults",
            "--json",
        ]
    ) == 0

    run_dir = runs_dir / "simple_directional" / "improved-defaults"
    config = (run_dir / "config.toml").read_text()
    metrics = json.loads((run_dir / "metrics.json").read_text())

    assert 'venue = "kalshi"' in config
    assert 'fill_timing = "next-tick"' in config
    assert "settle_on_market_rollover = true" in config
    assert "settlements_from_feed_db = true" in config
    assert metrics["venue"] == "kalshi"
    assert metrics["fill_timing"] == "next-tick"
    assert metrics["settlements_from_feed_db"] is True
    assert metrics["settled_positions"] >= 0


def test_replay_cli_rejects_polymarket_only_strategy_on_default_kalshi_venue(tmp_path: Path) -> None:
    feed_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_feed_db(feed_db)

    assert replay_main(
        [
            "--feed-db",
            str(feed_db),
            "--runs-dir",
            str(runs_dir),
            "--strategy",
            "hedge_volatility_v0",
            "--run-id",
            "wrong-venue",
            "--json",
        ]
    ) == 2


def test_replay_cli_accepts_max_open_positions_for_research_runs(tmp_path: Path) -> None:
    feed_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_feed_db(feed_db)

    assert replay_main(
        [
            "--feed-db",
            str(feed_db),
            "--runs-dir",
            str(runs_dir),
            "--strategy",
            "simple_directional",
            "--run-id",
            "wide-open",
            "--max-open-positions",
            "10",
            "--json",
        ]
    ) == 0

    run_dir = runs_dir / "simple_directional" / "wide-open"
    config = (run_dir / "config.toml").read_text()
    metrics = json.loads((run_dir / "metrics.json").read_text())
    with sqlite3.connect(run_dir / "results.sqlite3") as conn:
        fills = conn.execute("SELECT COUNT(*) FROM replay_fills").fetchone()[0]

    assert 'max_open_positions = 10' in config
    assert metrics["max_open_positions"] == 10
    assert "institutional_metrics" in metrics
    assert metrics["institutional_metrics"]["trades"] == fills
    assert "sharpe" in metrics["institutional_metrics"]
    assert "sortino" in metrics["institutional_metrics"]
    assert "calmar" in metrics["institutional_metrics"]
    assert "profit_factor" in metrics["institutional_metrics"]
    assert "max_drawdown_pct" in metrics["institutional_metrics"]
    assert metrics["institutional_metrics"]["settlement_source"] == "replay_final_snapshot"
    assert metrics["institutional_metrics"]["settled_trades"] == fills
    assert "replay_final_snapshot" in metrics["institutional_metrics"]["settlement_sources"]
    assert metrics["institutional_metrics"]["total_pnl"] != 0.0
    assert fills >= 1


def test_compute_metrics_returns_institutional_fields_for_fill_pnls() -> None:
    fills = [
        {"pnl": 12.0, "notional": 100.0},
        {"pnl": -4.0, "notional": 100.0},
        {"pnl": 8.0, "notional": 100.0},
        {"pnl": -2.0, "notional": 100.0},
        {"pnl": 6.0, "notional": 100.0},
    ]

    metrics = compute_metrics(fills, annualization=252)

    assert metrics["trades"] == 5
    assert metrics["wins"] == 3
    assert metrics["losses"] == 2
    assert metrics["notional"] == 500.0
    assert metrics["total_pnl"] == 20.0
    assert metrics["gross_profit"] == 26.0
    assert metrics["gross_loss"] == -6.0
    assert metrics["profit_factor"] == 26.0 / 6.0
    assert metrics["avg_win"] == 26.0 / 3.0
    assert metrics["avg_loss"] == -3.0
    assert metrics["largest_win"] == 12.0
    assert metrics["largest_loss"] == -4.0
    assert metrics["max_drawdown"] == 4.0
    assert metrics["max_drawdown_pct"] == 0.04
    assert metrics["sharpe"] > 0
    assert metrics["sortino"] > 0
    assert metrics["calmar"] > 0
    assert "skewness" in metrics
    assert "excess_kurtosis" in metrics


@dataclass(frozen=True)
class _Fill:
    market_ticker: str
    side: str
    entry_price: float
    notional: float
    contracts: float
    ts: datetime


def test_estimate_replay_fill_pnls_uses_final_snapshot_outcome() -> None:
    fills = [
        _Fill(
            market_ticker="KXBTC15M-TEST",
            side="long_above",
            entry_price=0.40,
            notional=20.0,
            contracts=50.0,
            ts=datetime(2026, 5, 15, 12, 14, 0, tzinfo=UTC),
        ),
        _Fill(
            market_ticker="KXBTC15M-TEST",
            side="long_below",
            entry_price=0.30,
            notional=30.0,
            contracts=100.0,
            ts=datetime(2026, 5, 15, 12, 14, 10, tzinfo=UTC),
        ),
    ]
    settlement_rows = [
        {"market_ticker": "KXBTC15M-TEST", "btc_price": 100_100.0, "strike": 100_000.0},
    ]

    settled = estimate_replay_fill_pnls(fills, settlement_rows)

    assert settled[0]["pnl"] == 30.0
    assert settled[0]["settlement_result"] == "above"
    assert settled[0]["exit_price"] == 1.0
    assert settled[0]["settlement_source"] == "replay_final_snapshot"
    assert settled[1]["pnl"] == -30.0
    assert settled[1]["settlement_result"] == "above"
    assert settled[1]["exit_price"] == 0.0


def test_portfolio_settlement_scores_inventory_by_market_not_fill() -> None:
    fills = [
        _Fill("KXBTC15M-TEST", "long_above", 0.56, 56.0, 100.0, datetime(2026, 5, 15, 12, 1, tzinfo=UTC)),
        _Fill("KXBTC15M-TEST", "long_below", 0.33, 33.0, 100.0, datetime(2026, 5, 15, 12, 2, tzinfo=UTC)),
    ]
    settlements = [
        {
            "market_ticker": "KXBTC15M-TEST",
            "winning_side": "yes",
            "source": "kalshi_api",
            "status": "settled_official",
        }
    ]

    settlement = compute_portfolio_settlement(fills, settlements)

    market = settlement["markets"][0]
    assert market["settlement_source"] == "kalshi_api"
    assert market["gross_payout"] == 100.0
    assert market["total_cost"] == 89.0
    assert market["realized_pnl"] == 11.0
    assert market["paired_cost"] == pytest.approx(0.89)
    assert market["raw_net_contracts"] == 0.0
    assert settlement["aggregate"]["realized_pnl"] == 11.0
    assert market["completed_pair_pnl"] == pytest.approx(11.0)
    assert market["unpaired_leftover_pnl"] == pytest.approx(0.0)
    assert settlement["aggregate"]["pnl_split"]["completed_pair_pnl"] == pytest.approx(11.0)


def test_kalshi_single_position_settlement_treats_opposing_buy_as_exit_flip() -> None:
    fills = [
        _Fill("KXBTC15M-TEST", "long_above", 0.56, 56.0, 100.0, datetime(2026, 5, 15, 12, 1, tzinfo=UTC)),
        _Fill("KXBTC15M-TEST", "long_below", 0.33, 33.0, 100.0, datetime(2026, 5, 15, 12, 2, tzinfo=UTC)),
    ]
    settlements = [
        {
            "market_ticker": "KXBTC15M-TEST",
            "winning_side": "yes",
            "source": "kalshi_api",
            "status": "settled_official",
        }
    ]

    settlement = compute_portfolio_settlement(fills, settlements, position_mode="kalshi_single_position")

    market = settlement["markets"][0]
    assert market["yes_contracts"] == 0.0
    assert market["no_contracts"] == 100.0
    assert market["total_cost"] == 33.0
    assert market["gross_payout"] == 0.0
    assert market["realized_pnl"] == -33.0
    assert market["completed_pair_contracts"] == 0.0


def test_portfolio_settlement_splits_completed_pairs_from_unpaired_leftovers() -> None:
    fills = [
        _Fill("KXBTC15M-TEST", "long_above", 20.0, 20.0, 100.0, datetime(2026, 5, 15, 12, 1, tzinfo=UTC)),
        _Fill("KXBTC15M-TEST", "long_below", 75.0, 75.0, 100.0, datetime(2026, 5, 15, 12, 2, tzinfo=UTC)),
        _Fill("KXBTC15M-TEST", "long_above", 6.0, 3.0, 50.0, datetime(2026, 5, 15, 12, 13, tzinfo=UTC)),
    ]
    settlements = [
        {"market_ticker": "KXBTC15M-TEST", "winning_side": "no", "source": "kalshi_api", "status": "settled_official"}
    ]

    settlement = compute_portfolio_settlement(fills, settlements)

    market = settlement["markets"][0]
    assert market["completed_pair_contracts"] == pytest.approx(100.0)
    assert market["completed_pair_cost"] == pytest.approx(95.0)
    assert market["completed_pair_pnl"] == pytest.approx(5.0)
    assert market["unpaired_leftover_cost"] == pytest.approx(3.0)
    assert market["unpaired_leftover_pnl"] == pytest.approx(-3.0)
    split = settlement["aggregate"]["pnl_split"]
    assert split["completed_pair_pnl"] == pytest.approx(5.0)
    assert split["unpaired_leftover_pnl"] == pytest.approx(-3.0)
    assert split["unpaired_yes_pnl"] == pytest.approx(-3.0)
    assert split["unpaired_time_buckets"]["yes:00-02m"]["pnl"] == pytest.approx(-3.0)


def test_replay_cli_enforces_capital_guardrails_and_reports_capital_metrics(tmp_path: Path) -> None:
    feed_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_feed_db(feed_db)

    assert replay_main(
        [
            "--feed-db",
            str(feed_db),
            "--runs-dir",
            str(runs_dir),
            "--strategy",
            "simple_directional",
            "--run-id",
            "bankroll-200",
            "--starting-bankroll",
            "200",
            "--max-capital-at-risk",
            "200",
            "--fee-per-contract",
            "0.01",
            "--per-market-max-exposure",
            "200",
            "--daily-loss-cap",
            "50",
            "--base-size-dollars",
            "150",
            "--max-position-dollars",
            "150",
            "--max-open-positions",
            "10",
            "--fill-timing",
            "same-tick",
            "--json",
        ]
    ) == 0

    run_dir = runs_dir / "simple_directional" / "bankroll-200"
    metrics = json.loads((run_dir / "metrics.json").read_text())
    config = (run_dir / "config.toml").read_text()
    with sqlite3.connect(run_dir / "results.sqlite3") as conn:
        fill_count = conn.execute("SELECT COUNT(*) FROM replay_fills").fetchone()[0]
        blocked_by = [row[0] for row in conn.execute("SELECT blocked_by_json FROM replay_signals")]

    assert 'starting_bankroll = 200.0' in config
    assert 'max_capital_at_risk = 200.0' in config
    assert 'fee_per_contract = 0.01' in config
    assert 'per_market_max_exposure = 200.0' in config
    assert 'daily_loss_cap = 50.0' in config
    assert fill_count == 1
    assert metrics["capital_metrics"]["cumulative_fees"] > 0
    assert any("capital_at_risk" in row or "bankroll_exhausted" in row for row in blocked_by)
    assert metrics["starting_bankroll"] == 200.0
    assert metrics["capital_metrics"]["starting_bankroll"] == 200.0
    assert metrics["capital_metrics"]["max_capital_used"] <= 200.0
    assert metrics["capital_metrics"]["return_on_max_capital_used"] is not None
    assert metrics["capital_metrics"]["ending_bankroll"] > 200.0


def test_replay_cli_writes_portfolio_settlement_outputs(tmp_path: Path) -> None:
    feed_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_feed_db(feed_db)

    assert replay_main(
        [
            "--feed-db",
            str(feed_db),
            "--runs-dir",
            str(runs_dir),
            "--strategy",
            "simple_inventory_mm",
            "--run-id",
            "portfolio-mm",
            "--max-open-positions",
            "10",
            "--max-position-dollars",
            "100",
            "--max-spread",
            "1.0",
            "--json",
        ]
    ) == 0

    run_dir = runs_dir / "simple_inventory_mm" / "portfolio-mm"
    metrics = json.loads((run_dir / "metrics.json").read_text())
    assert "portfolio_settlement" in metrics
    assert (run_dir / "portfolio_settlement.json").is_file()
    with sqlite3.connect(run_dir / "results.sqlite3") as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "portfolio_settlement_by_market" in tables


def test_replay_recycles_capital_after_market_rollover() -> None:
    from datetime import timedelta

    from kalshibtc.backtest.replay import ReplayEngine
    from kalshibtc.config import BotConfig, RiskLimits
    from kalshibtc.datafeed.models import OrderBookSnapshot, Tick
    from kalshibtc.execution.risk import CapitalConstraints, CapitalState, RiskManager
    from kalshibtc.market.contract import ContractWindow
    from kalshibtc.strategy.signals import Signal

    class AlwaysBuyYes:
        name = "always_buy_yes"

        def on_tick(self, state):
            return Signal(
                side="long_above",
                reason="test buy every contract",
                confidence=1.0,
                strategy=self.name,
                target_notional=90.0,
                allow_price_strike_mismatch=True,
            )

    close1 = datetime(2026, 5, 15, 12, 15, tzinfo=UTC)
    close2 = datetime(2026, 5, 15, 12, 30, tzinfo=UTC)
    ticks = [
        Tick(ts=close1 - timedelta(seconds=10), price=100_100, source="test"),
        Tick(ts=close2 - timedelta(seconds=10), price=100_200, source="test"),
    ]
    books = [
        OrderBookSnapshot(
            ts=ticks[0].ts,
            market_ticker="KXBTC15M-26MAY151215-15",
            yes_bid=0.89,
            yes_ask=0.90,
            no_bid=0.09,
            no_ask=0.10,
            raw={"strike": 100_000.0, "market_close_time": close1.isoformat()},
        ),
        OrderBookSnapshot(
            ts=ticks[1].ts,
            market_ticker="KXBTC15M-26MAY151230-30",
            yes_bid=0.89,
            yes_ask=0.90,
            no_bid=0.09,
            no_ask=0.10,
            raw={"strike": 100_000.0, "market_close_time": close2.isoformat()},
        ),
    ]
    capital = CapitalState(CapitalConstraints(starting_bankroll=100.0, max_capital_at_risk=100.0))
    report = ReplayEngine(
        config=BotConfig(),
        contract=ContractWindow(
            ticker="KXBTC15M-26MAY151215-15",
            strike=100_000.0,
            close_time=close1,
        ),
        strategies=[AlwaysBuyYes()],
        risk_manager=RiskManager(
            RiskLimits(base_size_dollars=90, max_position_dollars=90, max_open_positions=10, max_spread=1.0, min_confidence=0.0)
        ),
        capital_state=capital,
        settle_on_market_rollover=True,
    ).run(ticks=ticks, books=books)

    assert len(report.fills) == 2
    assert capital.locked_capital == pytest.approx(90.0)
    assert capital.realized_pnl == pytest.approx(10.0)
    assert capital.market_exposure == {"KXBTC15M-26MAY151230-30": pytest.approx(90.0)}


def test_replay_cli_reports_contract_and_daily_metrics(tmp_path: Path) -> None:
    feed_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    _write_feed_db(feed_db)

    assert replay_main(
        [
            "--feed-db",
            str(feed_db),
            "--runs-dir",
            str(runs_dir),
            "--strategy",
            "simple_inventory_mm",
            "--run-id",
            "portfolio-mm-by-period",
            "--max-open-positions",
            "10",
            "--max-position-dollars",
            "100",
            "--max-spread",
            "1.0",
            "--strategy-param",
            "seed_limit_price=0.58",
            "--strategy-param",
            "seed_contracts_per_side=10",
            "--strategy-param",
            "seed_add_contracts=10",
            "--strategy-param",
            "min_seconds_to_close=0",
            "--strategy-param",
            "max_seed_seconds_to_close=1000",
            "--fill-timing",
            "next-tick",
            "--json",
        ]
    ) == 0

    run_dir = runs_dir / "simple_inventory_mm" / "portfolio-mm-by-period"
    metrics = json.loads((run_dir / "metrics.json").read_text())
    assert metrics["contract_metrics"]["contracts"] >= 1
    assert metrics["contract_metrics"]["rows"][0]["market_ticker"] == "KXBTC15M-TEST"
    assert metrics["contract_metrics"]["rows"][0]["realized_pnl"] == pytest.approx(
        metrics["portfolio_settlement"]["realized_pnl"]
    )
    assert metrics["daily_metrics"]["days"] == 1
    assert metrics["daily_metrics"]["rows"][0]["date"] == "2026-05-15"
    assert metrics["daily_metrics"]["rows"][0]["contracts"] >= 1
    assert metrics["daily_metrics"]["rows"][0]["realized_pnl"] == pytest.approx(
        metrics["portfolio_settlement"]["realized_pnl"]
    )
    with sqlite3.connect(run_dir / "results.sqlite3") as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "contract_metrics" in tables
        assert "daily_metrics" in tables
        assert conn.execute("SELECT COUNT(*) FROM contract_metrics").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0] == 1
