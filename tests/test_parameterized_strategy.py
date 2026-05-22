from __future__ import annotations

from datetime import UTC, datetime, timedelta

from kalshibtc.datafeed.models import OrderBookSnapshot, Tick
from kalshibtc.market.contract import ContractWindow
from kalshibtc.market.state import MarketState
from kalshibtc.research.specs import CandidateSpec
from kalshibtc.strategy.parameterized_late_window import strategy_from_candidate_spec


def _state(*, seconds_to_close: float, price: float = 50_025.0, strike: float = 50_000.0) -> MarketState:
    close = datetime(2026, 5, 22, 12, 15, tzinfo=UTC)
    ts = close - timedelta(seconds=seconds_to_close)
    return MarketState(
        tick=Tick(ts=ts, price=price),
        orderbook=OrderBookSnapshot(
            ts=ts,
            market_ticker="KXBTCD-26MAY221215-B50000",
            yes_bid=0.58,
            yes_ask=0.60,
            no_bid=0.39,
            no_ask=0.41,
        ),
        contract=ContractWindow(
            ticker="KXBTCD-26MAY221215-B50000",
            strike=strike,
            close_time=close,
            open_time=close - timedelta(minutes=15),
        ),
        slope_30s=None,
    )


def _spec(**parameters: object) -> CandidateSpec:
    return CandidateSpec.from_mapping(
        {
            "name": "late_breakout_candidate",
            "economic_story": "Late BTC distance from strike may persist into settlement.",
            "mechanism": "parameterized_late_window",
            "parameters": parameters,
            "falsification": "Fails if official-settled replay cannot clear gates.",
            "created_at": "2026-05-22T12:00:00Z",
            "parent_id": "manual",
        }
    )


def test_parameterized_late_window_spec_emits_directional_signal_inside_window() -> None:
    strategy = strategy_from_candidate_spec(
        _spec(
            max_seconds_to_close=60,
            min_distance=10,
            max_entry_price=0.65,
            max_entry_spread=0.05,
            target_notional=15.0,
            base_confidence=0.7,
        )
    )

    signal = strategy.on_tick(_state(seconds_to_close=45, price=50_025.0))

    assert strategy.name == "candidate_late_breakout_candidate"
    assert signal.side == "long_above"
    assert signal.strategy == "candidate_late_breakout_candidate"
    assert signal.target_notional == 15.0
    assert signal.confidence >= 0.7
    assert signal.features == {
        "seconds_to_close": 45.0,
        "distance_from_strike": 25.0,
        "entry_price": 0.6,
        "entry_spread": 0.020000000000000018,
        "max_entry_price": 0.65,
        "max_entry_spread": 0.05,
        "min_distance": 10.0,
    }


def test_parameterized_late_window_blocks_outside_window_and_expensive_entries() -> None:
    strategy = strategy_from_candidate_spec(
        _spec(max_seconds_to_close=30, min_distance=10, max_entry_price=0.50)
    )

    outside = strategy.on_tick(_state(seconds_to_close=45, price=50_025.0))
    expensive = strategy.on_tick(_state(seconds_to_close=20, price=50_025.0))

    assert outside.side == "none"
    assert outside.reason == "outside candidate entry window"
    assert expensive.side == "none"
    assert expensive.reason == "candidate entry price too expensive"


def test_parameterized_late_window_rejects_unknown_mechanism() -> None:
    bad_spec = CandidateSpec.from_mapping(
        {
            "name": "bad_candidate",
            "economic_story": "No arbitrary generated code.",
            "mechanism": "import_os_and_trade_live",
            "parameters": {},
            "falsification": "Rejected before execution.",
            "created_at": "2026-05-22T12:00:00Z",
            "parent_id": "manual",
        }
    )

    try:
        strategy_from_candidate_spec(bad_spec)
    except ValueError as exc:
        assert "unsupported candidate mechanism" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("unsupported mechanism was accepted")
