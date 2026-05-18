from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from kalshibtc.polymarket_fill_validation import (
    FillValidationObservation,
    PolymarketBookLevel,
    PolymarketOrderBook,
    main,
    observe_fillability,
    parse_order_book,
    summarize_observations,
    write_observations_sqlite,
)


def _book(*, bids=None, asks=None, min_order_size="5") -> PolymarketOrderBook:
    payload = {
        "market": "0xmarket",
        "asset_id": "token-1",
        "timestamp": "123",
        "hash": "0xhash",
        "bids": bids if bids is not None else [{"price": "0.49", "size": "10"}],
        "asks": asks if asks is not None else [{"price": "0.51", "size": "10"}],
        "min_order_size": min_order_size,
        "tick_size": "0.01",
    }
    return parse_order_book(token_id="token-1", payload=payload)


def test_parse_order_book_sorts_levels_and_exposes_top_of_book() -> None:
    book = _book(
        bids=[{"price": "0.47", "size": "20"}, {"price": "0.49", "size": "10"}],
        asks=[{"price": "0.53", "size": "20"}, {"price": "0.51", "size": "10"}],
    )

    assert book.token_id == "token-1"
    assert book.best_bid == pytest.approx(0.49)
    assert book.best_ask == pytest.approx(0.51)
    assert book.spread == pytest.approx(0.02)
    assert book.bids == (PolymarketBookLevel(0.49, 10.0), PolymarketBookLevel(0.47, 20.0))
    assert book.asks == (PolymarketBookLevel(0.51, 10.0), PolymarketBookLevel(0.53, 20.0))


def test_observe_buy_fillability_detects_replay_touch_but_depth_shortfall() -> None:
    book = _book(asks=[{"price": "0.51", "size": "3"}, {"price": "0.52", "size": "1"}])

    obs = observe_fillability(book=book, side="BUY", limit_price=0.51, contracts=5)

    assert obs.replay_touch is True
    assert obs.fillable_contracts_at_limit == pytest.approx(3)
    assert obs.fully_fillable_at_limit is False
    assert obs.partial_fill_expected is True


def test_observe_sell_fillability_uses_bids_at_or_above_limit() -> None:
    book = _book(bids=[{"price": "0.48", "size": "2"}, {"price": "0.47", "size": "10"}])

    obs = observe_fillability(book=book, side="SELL", limit_price=0.47, contracts=5)

    assert obs.replay_touch is True
    assert obs.fillable_contracts_at_limit == pytest.approx(12)
    assert obs.fully_fillable_at_limit is True


def test_observe_fillability_honors_polymarket_min_order_size() -> None:
    book = _book(asks=[{"price": "0.51", "size": "10"}], min_order_size="5")

    obs = observe_fillability(book=book, side="BUY", limit_price=0.51, contracts=4)

    assert obs.replay_touch is True
    assert obs.fully_fillable_at_limit is False
    assert obs.partial_fill_expected is False


def test_summarize_observations_reports_replay_overstatement() -> None:
    observations = [
        observe_fillability(book=_book(asks=[{"price": "0.51", "size": "3"}]), side="BUY", limit_price=0.51, contracts=5),
        observe_fillability(book=_book(asks=[{"price": "0.50", "size": "8"}]), side="BUY", limit_price=0.51, contracts=5),
        observe_fillability(book=_book(asks=[{"price": "0.52", "size": "8"}]), side="BUY", limit_price=0.51, contracts=5),
    ]

    summary = summarize_observations(observations)

    assert summary.observations == 3
    assert summary.replay_touches == 2
    assert summary.full_fill_observations == 1
    assert summary.partial_fill_observations == 1
    assert summary.replay_overstates_full_fill_count == 1
    assert summary.avg_fillable_contracts_when_touched == pytest.approx(5.5)


def test_write_observations_sqlite_persists_rows(tmp_path: Path) -> None:
    obs = [observe_fillability(book=_book(), side="BUY", limit_price=0.51, contracts=5)]
    summary = summarize_observations(obs)
    db = tmp_path / "poly-fill.sqlite3"

    write_observations_sqlite(db, obs, summary)

    with sqlite3.connect(db) as conn:
        observation_count = conn.execute("SELECT COUNT(*) FROM polymarket_fill_observations").fetchone()[0]
        summary_count = conn.execute("SELECT COUNT(*) FROM polymarket_fill_summary").fetchone()[0]
    assert observation_count == 1
    assert summary_count == 1


def test_cli_refuses_place_order_even_with_confirmation(capsys) -> None:
    code = main(
        [
            "--token-id",
            "token-1",
            "--side",
            "BUY",
            "--limit-price",
            "0.51",
            "--place-order",
            "--allow-live-orders",
            "--confirm-polymarket-live-orders",
            "POLYMARKET_LIVE_ORDER_TEST",
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "refusing to place Polymarket orders" in captured.err


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class _Session:
    def get(self, url, **kwargs):
        assert url == "https://example.test/book"
        assert kwargs["params"] == {"token_id": "token-1"}
        return _Response(
            {
                "asset_id": "token-1",
                "bids": [{"price": "0.49", "size": "5"}],
                "asks": [{"price": "0.51", "size": "5"}],
                "min_order_size": "5",
                "tick_size": "0.01",
            }
        )


def test_cli_observe_dry_run_outputs_json(monkeypatch, capsys) -> None:
    from kalshibtc import polymarket_fill_validation as module

    monkeypatch.setattr(module.requests, "Session", lambda: _Session())
    code = main(
        [
            "--token-id",
            "token-1",
            "--side",
            "BUY",
            "--limit-price",
            "0.51",
            "--contracts",
            "5",
            "--samples",
            "1",
            "--clob-url",
            "https://example.test",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["summary"]["full_fill_rate"] == pytest.approx(1.0)
