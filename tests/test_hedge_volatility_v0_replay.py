from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from kalshibtc.replay.hedge_volatility_v0 import evaluate_settlement, main, run_replay


def create_feed_db(path):
    con = sqlite3.connect(path)
    con.execute(
        """
        CREATE TABLE realtime_snapshots_1s (
            ts TEXT NOT NULL,
            market_ticker TEXT NOT NULL,
            market_open_time TEXT,
            market_close_time TEXT NOT NULL,
            btc_price REAL NOT NULL,
            strike REAL NOT NULL,
            target_price REAL,
            distance_from_strike REAL,
            seconds_to_close REAL,
            btc_velocity_30s REAL,
            slope_30s REAL,
            yes_bid REAL,
            yes_ask REAL,
            no_bid REAL,
            no_ask REAL,
            orderbook_sequence INTEGER,
            execution_blocked_by_json TEXT NOT NULL,
            raw_state_json TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (market_ticker, ts)
        )
        """
    )
    base = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    rows = [
        # Seed is allowed even though yes+no ask cost is above target_pair_cost.
        (base + timedelta(minutes=1), "KXBTCD-26MAY151215-T50000", 50120, 50000, 0.62, 0.43, 8.0),
        # YES does not improve, NO improves combined cost.
        (base + timedelta(minutes=2), "KXBTCD-26MAY151215-T50000", 50140, 50000, 0.63, 0.22, 7.0),
        # New market with seed pair cost above seed_max_pair_cost -> reject.
        (base + timedelta(minutes=16), "KXBTCD-26MAY151230-T50250", 50280, 50250, 0.70, 0.45, 6.0),
    ]
    for ts, ticker, price, strike, yes_ask, no_ask, slope in rows:
        close = base + (timedelta(minutes=15) if "1215" in ticker else timedelta(minutes=30))
        con.execute(
            """
            INSERT INTO realtime_snapshots_1s (
                ts, market_ticker, market_open_time, market_close_time, btc_price, strike,
                target_price, distance_from_strike, seconds_to_close, btc_velocity_30s,
                slope_30s, yes_bid, yes_ask, no_bid, no_ask, orderbook_sequence,
                execution_blocked_by_json, raw_state_json, raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts.isoformat(),
                ticker,
                base.isoformat(),
                close.isoformat(),
                price,
                strike,
                strike,
                price - strike,
                (close - ts).total_seconds(),
                slope,
                slope,
                yes_ask - 0.02,
                yes_ask,
                no_ask - 0.02,
                no_ask,
                1,
                "[]",
                "{}",
                json.dumps({"test": True}),
                ts.isoformat(),
            ),
        )
    con.commit()


def test_run_replay_writes_results_db_and_summary(tmp_path):
    feed_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    create_feed_db(feed_db)

    summary = run_replay(
        feed_db=feed_db,
        runs_dir=runs_dir,
        run_id="unit",
        from_ts=None,
        to_ts=None,
    )

    run_dir = runs_dir / "replay" / "hedge_volatility_v0" / "unit"
    results_db = run_dir / "results.sqlite3"
    assert results_db.exists()
    assert (run_dir / "metrics.json").exists()
    assert summary["markets_processed"] == 2
    assert summary["snapshots_processed"] == 3
    assert summary["fills"] == 3
    assert summary["final_yes_contracts"] == 3
    assert summary["final_no_contracts"] == 3
    assert summary["final_unpaired_yes_contracts"] == 0
    assert summary["final_unpaired_no_contracts"] == 0
    assert summary["max_unpaired_contracts_seen"] <= 1
    assert summary["imbalance_rejects"] == 0
    assert summary["avg_yes_entry"] == 0.62
    assert summary["avg_no_entry"] == 0.36
    assert summary["combined_average_cost"] == 0.98
    assert summary["locked_edge_per_pair"] == 0.02
    assert summary["orderbook_snapshots_logged"] == 3
    assert summary["seed_fills"] == 2
    assert summary["add_fills"] == 1
    assert summary["final_paired_cost"] == 0.98
    assert summary["buy_both_cost_min"] == 0.85
    assert summary["buy_both_cost_max"] == 1.15
    assert summary["buy_both_cost_mean"] == 1.016667
    assert summary["reject_counts_by_reason"] == {
        "seed_pair_cost_too_high": 1,
        "yes_price_not_improved": 1,
    }

    con = sqlite3.connect(results_db)
    assert con.execute("select count(*) from hedge_orderbook_snapshots").fetchone()[0] == 3
    assert con.execute("select count(*) from hedge_fills").fetchone()[0] == 3
    assert con.execute("select count(*) from hedge_decisions where decision = 'REJECT'").fetchone()[0] == 2
    assert con.execute("select count(*) from hedge_positions").fetchone()[0] == 2


def test_cli_prints_summary_json(tmp_path, capsys):
    feed_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    create_feed_db(feed_db)

    code = main(
        [
            "--feed-db",
            str(feed_db),
            "--runs-dir",
            str(runs_dir),
            "--run-id",
            "cli",
            "--json",
        ]
    )

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["run_dir"].endswith("runs/replay/hedge_volatility_v0/cli")
    assert out["fills"] == 3
    assert out["reject_counts_by_reason"]["seed_pair_cost_too_high"] == 1


def test_settlement_yes_wins_and_paired_hedge_below_one_is_profitable():
    position = _position(yes_price=0.55, yes_contracts=3, no_price=0.39, no_contracts=2)

    settlement = evaluate_settlement(
        positions={"KXBTCD-TEST-T50000": position},
        strikes={"KXBTCD-TEST-T50000": 50_000.0},
        settlement_price=50_100.0,
        settlement_source="manual",
    )

    assert settlement["settlement_price"] == 50100.0
    assert settlement["settlement_source"] == "manual"
    assert settlement["winning_side"] == "yes"
    assert settlement["gross_payout"] == 3.0
    assert settlement["total_cost"] == 2.43
    assert settlement["realized_pnl"] == 0.57
    assert settlement["paired_contracts"] == 2.0
    assert settlement["unpaired_yes_contracts"] == 1.0
    assert settlement["unpaired_no_contracts"] == 0.0
    assert settlement["paired_locked_edge"] == 0.12
    assert settlement["unpaired_directional_pnl"] == 0.45


def test_settlement_no_wins():
    position = _position(yes_price=0.55, yes_contracts=3, no_price=0.39, no_contracts=2)

    settlement = evaluate_settlement(
        positions={"KXBTCD-TEST-T50000": position},
        strikes={"KXBTCD-TEST-T50000": 50_000.0},
        settlement_price=49_999.0,
        settlement_source="coinbase_1m_avg",
    )

    assert settlement["settlement_source"] == "coinbase_1m_avg"
    assert settlement["winning_side"] == "no"
    assert settlement["gross_payout"] == 2.0
    assert settlement["total_cost"] == 2.43
    assert settlement["realized_pnl"] == -0.43
    assert settlement["paired_locked_edge"] == 0.12
    assert settlement["unpaired_directional_pnl"] == -0.55


def test_settlement_unpaired_no_inventory_affects_pnl_correctly():
    position = _position(yes_price=0.52, yes_contracts=2, no_price=0.41, no_contracts=4)

    settlement = evaluate_settlement(
        positions={"KXBTCD-TEST-T50000": position},
        strikes={"KXBTCD-TEST-T50000": 50_000.0},
        settlement_price=50_250.0,
        settlement_source="chainlink_1m_avg",
    )

    assert settlement["winning_side"] == "yes"
    assert settlement["gross_payout"] == 2.0
    assert settlement["total_cost"] == 2.68
    assert settlement["realized_pnl"] == -0.68
    assert settlement["paired_contracts"] == 2.0
    assert settlement["unpaired_yes_contracts"] == 0.0
    assert settlement["unpaired_no_contracts"] == 2.0
    assert settlement["paired_locked_edge"] == 0.14
    assert settlement["unpaired_directional_pnl"] == -0.82


def test_run_replay_writes_settlement_json_when_manual_price_is_provided(tmp_path):
    feed_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    create_feed_db(feed_db)

    summary = run_replay(
        feed_db=feed_db,
        runs_dir=runs_dir,
        run_id="settled",
        from_ts=None,
        to_ts=None,
        settlement_price=50_100.0,
        settlement_source="kalshi",
    )

    run_dir = runs_dir / "replay" / "hedge_volatility_v0" / "settled"
    settlement = json.loads((run_dir / "settlement.json").read_text())
    assert summary["settlement_price"] == 50100.0
    assert summary["settlement_source"] == "kalshi"
    assert summary["winning_side"] == "yes"
    assert settlement["realized_pnl"] == summary["realized_pnl"]
    assert settlement["paired_locked_edge"] == 0.06


def test_cli_accepts_settlement_args(tmp_path, capsys):
    feed_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    create_feed_db(feed_db)

    code = main(
        [
            "--feed-db",
            str(feed_db),
            "--runs-dir",
            str(runs_dir),
            "--run-id",
            "cli-settled",
            "--settlement-price",
            "50100",
            "--settlement-source",
            "manual",
            "--json",
        ]
    )

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["settlement_price"] == 50100.0
    assert out["settlement_source"] == "manual"
    assert out["winning_side"] == "yes"


def _position(*, yes_price: float, yes_contracts: float, no_price: float, no_contracts: float):
    from kalshibtc.portfolio.hedge_position import HedgePosition

    position = HedgePosition(market_ticker="KXBTCD-TEST-T50000")
    now = datetime.now(UTC)
    position.add_fill(side="yes", price=yes_price, contracts=yes_contracts, ts=now, reason="test")
    position.add_fill(side="no", price=no_price, contracts=no_contracts, ts=now, reason="test")
    return position


def test_bad_strike_rows_are_ignored_by_default_bounds(tmp_path):
    feed_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    create_feed_db(feed_db)
    con = sqlite3.connect(feed_db)
    row = con.execute("select * from realtime_snapshots_1s where market_ticker = ? order by ts limit 1", ("KXBTCD-26MAY151215-T50000",)).fetchone()
    bad = list(row)
    bad[0] = "2026-05-15T12:00:30+00:00"
    bad[5] = 15.0
    con.execute("insert into realtime_snapshots_1s values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", bad)
    con.commit()

    summary = run_replay(
        feed_db=feed_db,
        runs_dir=runs_dir,
        run_id="bad-strike",
        from_ts=None,
        to_ts=None,
    )

    assert summary["snapshots_processed"] == 3
    assert summary["skipped_bad_strike_rows"] == 1
    assert summary["min_strike"] == 1000
    assert summary["max_strike"] == 1000000


def test_market_ticker_filter_limits_replay_to_one_ticker(tmp_path):
    feed_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    create_feed_db(feed_db)

    summary = run_replay(
        feed_db=feed_db,
        runs_dir=runs_dir,
        run_id="one-ticker",
        from_ts=None,
        to_ts=None,
        market_ticker="KXBTCD-26MAY151215-T50000",
    )

    assert summary["market_ticker_filter"] == "KXBTCD-26MAY151215-T50000"
    assert summary["markets_processed"] == 1
    assert summary["snapshots_processed"] == 2
    assert summary["fills"] == 3
    assert summary["reject_counts_by_reason"] == {"yes_price_not_improved": 1}


def test_real_strike_rows_still_replay_with_bounds(tmp_path):
    feed_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    create_feed_db(feed_db)

    summary = run_replay(
        feed_db=feed_db,
        runs_dir=runs_dir,
        run_id="real-strike",
        from_ts=None,
        to_ts=None,
        min_strike=49_000,
        max_strike=51_000,
    )

    assert summary["snapshots_processed"] == 3
    assert summary["fills"] == 3
    assert summary["skipped_bad_strike_rows"] == 0


def test_per_contract_replay_processes_one_market_and_logs_expected_snapshot_count(tmp_path):
    feed_db = tmp_path / "feed.sqlite3"
    runs_dir = tmp_path / "runs"
    create_feed_db(feed_db)

    summary = run_replay(
        feed_db=feed_db,
        runs_dir=runs_dir,
        run_id="contract-count",
        from_ts=None,
        to_ts=None,
        market_ticker="KXBTCD-26MAY151215-T50000",
    )

    assert summary["markets_processed"] == 1
    assert summary["snapshots_processed"] == 2
    assert summary["orderbook_snapshots_logged"] == 2
    assert summary["seed_fills"] == 2
    assert summary["add_fills"] == 1
