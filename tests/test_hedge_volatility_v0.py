from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

import pytest

from kalshibtc.datafeed.models import OrderBookSnapshot, Tick
from kalshibtc.execution.hedge_paper import HedgePaperExecutor
from kalshibtc.market.contract import ContractWindow
from kalshibtc.market.state import MarketState
from kalshibtc.portfolio.hedge_position import HedgePosition
from kalshibtc.strategy.hedge_volatility_v0 import HedgeVolatilityConfig, HedgeVolatilityV0


def make_state(
    *,
    ts: datetime = datetime(2026, 5, 15, 12, 5, tzinfo=UTC),
    price: float = 50_150.0,
    strike: float = 50_000.0,
    close_time: datetime = datetime(2026, 5, 15, 12, 15, tzinfo=UTC),
    slope_30s: float = 10.0,
    yes_bid: float = 0.53,
    yes_ask: float = 0.55,
    no_bid: float = 0.38,
    no_ask: float = 0.40,
    ticker: str = "KXBTCD-26MAY151215-T50000",
) -> MarketState:
    raw = {
        "market_open_time": "2026-05-15T12:00:00+00:00",
        "market_close_time": close_time.isoformat(),
        "strike": strike,
    }
    return MarketState(
        tick=Tick(ts=ts, price=price, source="test", raw={"price": price}),
        orderbook=OrderBookSnapshot(
            ts=ts,
            market_ticker=ticker,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            raw=raw,
        ),
        contract=ContractWindow(ticker=ticker, strike=strike, close_time=close_time),
        slope_30s=slope_30s,
    )


def test_position_tracks_yes_no_average_cost_and_locked_edge():
    position = HedgePosition(market_ticker="KXBTC")
    position.add_fill(side="yes", price=0.57, contracts=3, ts=datetime.now(UTC), reason="seed")
    position.add_fill(side="no", price=0.37, contracts=2, ts=datetime.now(UTC), reason="seed")

    assert position.yes_contracts == 3
    assert position.no_contracts == 2
    assert position.avg_yes_entry == pytest.approx(0.57)
    assert position.avg_no_entry == pytest.approx(0.37)
    assert position.combined_average_cost == pytest.approx(0.94)
    assert position.locked_edge_per_pair == pytest.approx(0.06)
    assert position.would_improve(side="no", price=0.36)
    assert not position.would_improve(side="no", price=0.38)


def test_strategy_seeds_three_to_two_with_trend_when_pair_cost_is_safe(caplog):
    strategy = HedgeVolatilityV0(HedgeVolatilityConfig(max_projected_pair_cost=0.95))
    position = HedgePosition(market_ticker="KXBTC")
    state = make_state(yes_ask=0.55, no_ask=0.39, slope_30s=9.0)

    with caplog.at_level(logging.INFO):
        decisions = strategy.decide(state, position)

    assert [(d.side, d.contracts, d.price) for d in decisions] == [("yes", 3, 0.55), ("no", 2, 0.39)]
    assert all(d.projected_combined_average_cost == pytest.approx(0.94) for d in decisions)
    assert "decision=ALLOW" in caplog.text
    assert "seed_3_to_2_trend_yes" in caplog.text


def test_strategy_rejects_seed_when_combined_cost_is_above_threshold(caplog):
    strategy = HedgeVolatilityV0(HedgeVolatilityConfig(max_projected_pair_cost=0.95))
    position = HedgePosition(market_ticker="KXBTC")
    state = make_state(yes_ask=0.58, no_ask=0.40, slope_30s=9.0)

    with caplog.at_level(logging.INFO):
        decisions = strategy.decide(state, position)

    assert decisions == []
    assert "decision=REJECT" in caplog.text
    assert "projected_combined_cost_too_high" in caplog.text


def test_strategy_adds_only_when_projected_combined_average_cost_improves():
    strategy = HedgeVolatilityV0(HedgeVolatilityConfig(max_projected_pair_cost=0.95))
    position = HedgePosition(market_ticker="KXBTC")
    now = datetime.now(UTC)
    position.add_fill(side="yes", price=0.55, contracts=3, ts=now, reason="seed")
    position.add_fill(side="no", price=0.39, contracts=2, ts=now, reason="seed")
    state = make_state(yes_ask=0.56, no_ask=0.38, slope_30s=8.0)

    decisions = strategy.decide(state, position)

    assert [(d.side, d.contracts, d.price) for d in decisions] == [("no", 1, 0.38)]
    assert decisions[0].projected_combined_average_cost < 0.94


def test_strategy_uses_volatility_distance_and_time_gates():
    strategy = HedgeVolatilityV0(
        HedgeVolatilityConfig(
            max_projected_pair_cost=0.95,
            min_abs_slope=5.0,
            min_recent_volatility=4.0,
            min_distance_from_strike=25.0,
            min_seconds_to_expiry=60.0,
            max_seconds_to_expiry=14 * 60.0,
        )
    )
    position = HedgePosition(market_ticker="KXBTC")

    assert strategy.decide(make_state(slope_30s=2.0), position) == []
    assert strategy.decide(make_state(price=50_010.0, slope_30s=8.0), position) == []
    assert strategy.decide(
        make_state(
            ts=datetime(2026, 5, 15, 12, 14, 30, tzinfo=UTC),
            close_time=datetime(2026, 5, 15, 12, 15, tzinfo=UTC),
            slope_30s=8.0,
        ),
        position,
    ) == []
    assert strategy.decide(
        make_state(
            ts=datetime(2026, 5, 15, 12, 0, 10, tzinfo=UTC),
            close_time=datetime(2026, 5, 15, 12, 15, tzinfo=UTC),
            slope_30s=8.0,
        ),
        position,
    ) == []
    assert strategy.decide(make_state(slope_30s=8.0), position, recent_volatility=1.0) == []


def test_paper_executor_records_decisions_and_positions_to_results_db(tmp_path):
    db = tmp_path / "results.sqlite3"
    strategy = HedgeVolatilityV0(HedgeVolatilityConfig(max_projected_pair_cost=0.95))
    executor = HedgePaperExecutor(results_db=db, strategy_name=strategy.name)
    state = make_state(yes_ask=0.55, no_ask=0.39, slope_30s=9.0)
    position = HedgePosition(market_ticker=state.orderbook.market_ticker)
    decisions = strategy.decide(state, position)

    with executor:
        for decision in decisions:
            executor.apply(state=state, position=position, decision=decision)
        executor.record_no_trade(
            state=make_state(ts=datetime(2026, 5, 15, 12, 6, tzinfo=UTC), yes_ask=0.58, no_ask=0.40),
            reason="projected_combined_cost_too_high",
            position=position,
        )

    con = sqlite3.connect(db)
    fill_count = con.execute("select count(*) from hedge_fills").fetchone()[0]
    decision_rows = con.execute(
        "select decision, reason from hedge_decisions order by id"
    ).fetchall()
    position_row = con.execute(
        "select yes_contracts, no_contracts, avg_yes_entry, avg_no_entry, combined_average_cost from hedge_positions"
    ).fetchone()

    assert fill_count == 2
    assert decision_rows == [
        ("ALLOW", "seed_3_to_2_trend_yes"),
        ("ALLOW", "seed_3_to_2_trend_yes"),
        ("REJECT", "projected_combined_cost_too_high"),
    ]
    assert position_row == pytest.approx((3, 2, 0.55, 0.39, 0.94))
