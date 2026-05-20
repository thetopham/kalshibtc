from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kalshibtc.execution.paper_inventory import PaperInventoryPosition
from kalshibtc.strategy.dynamic_complement_hedge import (
    DynamicComplementHedgeBot,
    DynamicHedgeConfig,
    Snapshot,
    is_valid_book,
    replay_feed_db,
)


def _ts(seconds: int) -> datetime:
    return datetime(2026, 5, 15, 12, 0, tzinfo=UTC) + timedelta(seconds=seconds)


def _snap(
    *,
    ts: datetime | None = None,
    ticker: str = "KXBTC15M-TEST",
    price: float = 100_010.0,
    strike: float = 100_000.0,
    seconds_to_close: float = 780.0,
    yes_bid: float = 0.53,
    yes_ask: float = 0.55,
    no_bid: float = 0.43,
    no_ask: float = 0.45,
    slope: float = 2.0,
) -> Snapshot:
    close = _ts(900)
    return Snapshot(
        ts=ts or _ts(120),
        market_ticker=ticker,
        market_open_time=_ts(0),
        market_close_time=close,
        btc_price=price,
        strike=strike,
        distance_from_strike=price - strike,
        seconds_to_close=seconds_to_close,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        slope=slope,
    )


def test_locked_edge_math_and_equal_yes_no_settlement_guarantee() -> None:
    position = PaperInventoryPosition("KXBTC15M-TEST")
    position.add_fill(side="YES", price=0.56, shares=100, ts=_ts(120), fee=0.0)
    position.add_fill(side="NO", price=0.16, shares=100, ts=_ts(180), fee=0.0)

    assert position.yes_shares == 100
    assert position.no_shares == 100
    assert position.matched_shares == 100
    assert position.directional_exposure == 0
    assert position.total_cost == 72.0
    assert position.locked_cost == 72.0
    assert position.locked_edge == 28.0
    assert position.settle("YES").pnl == 28.0
    assert position.settle("NO").pnl == 28.0
    assert position.worst_case_pnl() == 28.0


def test_partial_hedge_accounting_uses_allocated_basis() -> None:
    position = PaperInventoryPosition("KXBTC15M-TEST")
    position.add_fill(side="YES", price=0.56, shares=100, ts=_ts(120), fee=0.0)
    position.add_fill(side="NO", price=0.16, shares=25, ts=_ts(180), fee=0.0)

    assert position.matched_shares == 25
    assert position.directional_exposure == 75
    assert position.locked_cost == 18.0
    assert position.locked_edge == 7.0
    assert position.max_payout_if_yes == 100.0
    assert position.max_payout_if_no == 25.0
    assert position.worst_case_settlement_value == 25.0
    assert position.worst_case_pnl() == -35.0


def test_invalid_book_rejection() -> None:
    assert is_valid_book(_snap()) is True
    assert is_valid_book(_snap(yes_bid=None)) is False
    assert is_valid_book(_snap(yes_bid=0.0)) is False
    assert is_valid_book(_snap(yes_ask=1.01)) is False
    assert is_valid_book(_snap(yes_bid=0.57, yes_ask=0.55)) is False
    assert is_valid_book(_snap(yes_bid=0.62, no_bid=0.42)) is False


def test_dynamic_hedge_bot_initial_entry_and_persistent_partial_hedge() -> None:
    bot = DynamicComplementHedgeBot(DynamicHedgeConfig(slippage=0.0, cooldown_seconds=0))
    first = bot.on_snapshot(_snap(ts=_ts(120), yes_ask=0.55, no_ask=0.45, slope=3.0))
    assert first.fill is not None
    assert first.fill.side == "YES"
    assert first.position.yes_shares == 100

    # First cheap NO snapshot only starts edge persistence; no fill yet.
    first_cheap = bot.on_snapshot(
        _snap(ts=_ts(130), price=100_100, yes_bid=0.82, yes_ask=0.84, no_bid=0.13, no_ask=0.15, slope=1.0)
    )
    assert first_cheap.fill is None

    second_cheap = bot.on_snapshot(
        _snap(ts=_ts(131), price=100_100, yes_bid=0.82, yes_ask=0.84, no_bid=0.13, no_ask=0.15, slope=1.0)
    )
    assert second_cheap.fill is not None
    assert second_cheap.fill.side == "NO"
    assert second_cheap.fill.shares == 25
    assert second_cheap.position.no_shares == 25
    assert second_cheap.position.locked_edge == 7.5


def test_dynamic_hedge_bot_does_not_overhedge_or_trade_final_cutoffs() -> None:
    bot = DynamicComplementHedgeBot(DynamicHedgeConfig(slippage=0.0, cooldown_seconds=0))
    assert bot.on_snapshot(_snap(ts=_ts(820), seconds_to_close=80)).fill is None

    bot.on_snapshot(_snap(ts=_ts(120), seconds_to_close=780, yes_ask=0.55, slope=3.0))
    too_late_hedge = bot.on_snapshot(_snap(ts=_ts(886), seconds_to_close=14, no_ask=0.10, yes_ask=0.88))
    assert too_late_hedge.fill is None


def _write_dynamic_feed_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
                distance_from_strike REAL,
                seconds_to_close REAL,
                slope_30s REAL,
                yes_bid REAL,
                yes_ask REAL,
                no_bid REAL,
                no_ask REAL,
                raw_json TEXT,
                PRIMARY KEY (market_ticker, ts)
            )
            """
        )
        rows = [
            (_ts(60), "A", 100_010, 100_000, 840, 2.0, 0.53, 0.55, 0.43, 0.45),
            (_ts(120), "A", 100_020, 100_000, 780, 3.0, 0.54, 0.56, 0.42, 0.44),
            (_ts(180), "A", 100_100, 100_000, 720, 1.0, 0.82, 0.85, 0.13, 0.15),
            (_ts(181), "A", 100_100, 100_000, 719, 1.0, 0.82, 0.85, 0.13, 0.15),
            (_ts(899), "A", 100_110, 100_000, 1, 0.0, 0.90, 0.95, 0.04, 0.08),
        ]
        for ts, ticker, price, strike, stc, slope, yb, ya, nb, na in rows:
            conn.execute(
                """
                INSERT INTO realtime_snapshots_1s VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts.isoformat(),
                    ticker,
                    _ts(0).isoformat(),
                    _ts(900).isoformat(),
                    price,
                    strike,
                    price - strike,
                    stc,
                    slope,
                    yb,
                    ya,
                    nb,
                    na,
                    "{}",
                ),
            )


def test_replay_feed_db_writes_read_only_run_artifacts_without_real_trades(tmp_path: Path) -> None:
    feed_db = tmp_path / "feed.sqlite3"
    out_dir = tmp_path / "run"
    _write_dynamic_feed_db(feed_db)

    summary = replay_feed_db(feed_db, out_dir=out_dir, config=DynamicHedgeConfig(slippage=0.0, cooldown_seconds=0))

    assert summary["markets_tested"] == 1
    assert summary["markets_traded"] == 1
    assert summary["markets_hedged"] == 1
    assert summary["fills"] == 2
    assert summary["paper_only"] is True
    assert summary["total_paper_pnl"] == 41.25
    assert (out_dir / "dynamic_hedge_results.csv").is_file()
    assert (out_dir / "metrics.json").is_file()
    assert json.loads((out_dir / "metrics.json").read_text())["paper_only"] is True

    with sqlite3.connect(feed_db) as conn:
        assert {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"realtime_snapshots_1s"}
