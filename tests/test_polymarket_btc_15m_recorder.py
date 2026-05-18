from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from kalshibtc.polymarket_btc_15m_recorder import (
    PolymarketBTC15mRecorder,
    PolymarketBTC15mSnapshotSource,
    snapshot_row_from_payload,
)
from kalshibtc.polymarket_chainlink import ChainlinkPriceStore, ChainlinkPriceTick

BASE_TS = datetime(2026, 5, 18, 4, 45, 1, 123456, tzinfo=UTC)
OPEN_TS = datetime(2026, 5, 18, 4, 45, tzinfo=UTC)
CLOSE_TS = datetime(2026, 5, 18, 5, 0, tzinfo=UTC)


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "ts": BASE_TS.isoformat(),
        "market_slug": "btc-updown-15m-1779089400",
        "condition_id": "0xcondition",
        "yes_token_id": "111",
        "no_token_id": "222",
        "market_open_time": OPEN_TS.isoformat(),
        "market_close_time": CLOSE_TS.isoformat(),
        "btc_price": 103_250.25,
        "strike": 103_000.0,
        "target_price": 103_000.0,
        "yes_bid": 0.49,
        "yes_ask": 0.51,
        "no_bid": 0.48,
        "no_ask": 0.52,
        "yes_orderbook": {"bids": [{"price": "0.49", "size": "20"}], "asks": [{"price": "0.51", "size": "15"}], "timestamp": "1779089101"},
        "no_orderbook": {"bids": [{"price": "0.48", "size": "20"}], "asks": [{"price": "0.52", "size": "15"}], "timestamp": "1779089101"},
        "raw_market": {"slug": "btc-updown-15m-1779089400", "question": "Bitcoin Up or Down"},
        "raw_book": {"yes": {"asset_id": "111"}, "no": {"asset_id": "222"}},
        "btc_velocity_30s": 2.5,
    }
    payload.update(overrides)
    return payload


def _row(db_path: Path) -> sqlite3.Row:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM realtime_snapshots_1s").fetchone()


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class FakeSession:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.headers: dict[str, str] = {}
        self.gets: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.gets.append({"url": url, **kwargs})
        return FakeResponse(self.responses.pop(0))


def test_polymarket_recorder_initializes_requested_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "polymarket-btc-1s.sqlite3"

    PolymarketBTC15mRecorder(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(realtime_snapshots_1s)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(realtime_snapshots_1s)")}

    assert {
        "ts",
        "market_slug",
        "condition_id",
        "yes_token_id",
        "no_token_id",
        "market_open_time",
        "market_close_time",
        "seconds_to_close",
        "btc_price",
        "strike",
        "target_price",
        "yes_bid",
        "yes_ask",
        "no_bid",
        "no_ask",
        "yes_orderbook_json",
        "no_orderbook_json",
        "raw_market_json",
        "raw_book_json",
        "raw_json",
        "market_ticker",
    } <= columns
    assert "idx_poly_realtime_snapshots_1s_ts" in indexes
    assert "idx_poly_realtime_snapshots_1s_market_close" in indexes


def test_polymarket_recorder_can_add_nullable_columns_to_existing_nonempty_table(tmp_path: Path) -> None:
    db_path = tmp_path / "mixed-feed.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE realtime_snapshots_1s (ts TEXT NOT NULL, market_ticker TEXT NOT NULL)")
        conn.execute("INSERT INTO realtime_snapshots_1s (ts, market_ticker) VALUES ('2026-05-18T00:00:00+00:00', 'legacy')")

    PolymarketBTC15mRecorder(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(realtime_snapshots_1s)")}
        count = conn.execute("SELECT COUNT(*) FROM realtime_snapshots_1s").fetchone()[0]

    assert count == 1
    assert "market_slug" in columns
    assert "raw_book_json" in columns


def test_polymarket_recorder_writes_one_snapshot_row(tmp_path: Path) -> None:
    db_path = tmp_path / "polymarket-btc-1s.sqlite3"
    recorder = PolymarketBTC15mRecorder(db_path)

    assert recorder.record_snapshot(_payload()) is True

    row = _row(db_path)
    assert row["ts"] == "2026-05-18T04:45:01+00:00"
    assert row["market_slug"] == "btc-updown-15m-1779089400"
    assert row["market_ticker"] == "btc-updown-15m-1779089400"
    assert row["condition_id"] == "0xcondition"
    assert row["yes_token_id"] == "111"
    assert row["no_token_id"] == "222"
    assert row["seconds_to_close"] == 899.0
    assert row["btc_price"] == 103_250.25
    assert row["strike"] == 103_000.0
    assert row["yes_ask"] == 0.51
    assert '"asset_id": "111"' in row["raw_book_json"]
    assert '"venue": "polymarket"' in row["raw_state_json"]


def test_duplicate_polymarket_snapshot_upserts_by_slug_and_second(tmp_path: Path) -> None:
    db_path = tmp_path / "polymarket-btc-1s.sqlite3"
    recorder = PolymarketBTC15mRecorder(db_path)

    recorder.record_snapshot(_payload(btc_price=103_001, yes_ask=0.51))
    recorder.record_snapshot(_payload(ts="2026-05-18T04:45:01.900000+00:00", btc_price=103_009, yes_ask=0.54))

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        count = conn.execute("SELECT COUNT(*) FROM realtime_snapshots_1s").fetchone()[0]
        row = conn.execute("SELECT * FROM realtime_snapshots_1s").fetchone()

    assert count == 1
    assert row["btc_price"] == 103_009
    assert row["yes_ask"] == 0.54


def test_snapshot_source_fetches_event_books_and_btc_price() -> None:
    event = {
        "slug": "btc-updown-15m-1779089400",
        "title": "Bitcoin Up or Down - May 17, 10:45PM-11:00PM ET",
        "markets": [
            {
                "slug": "btc-updown-15m-1779089400",
                "conditionId": "0xcondition",
                "clobTokenIds": '["111", "222"]',
                "outcomes": '["Yes", "No"]',
                "startDate": "2026-05-18T04:45:00Z",
                "endDate": "2999-05-18T05:00:00Z",
                "question": "Bitcoin Up or Down - above $103,000?",
            }
        ],
    }
    yes_book = {
        "market": "0xcondition",
        "asset_id": "111",
        "timestamp": "1779089101",
        "bids": [{"price": "0.49", "size": "20"}],
        "asks": [{"price": "0.51", "size": "15"}],
    }
    no_book = {
        "market": "0xcondition",
        "asset_id": "222",
        "timestamp": "1779089101",
        "bids": [{"price": "0.48", "size": "20"}],
        "asks": [{"price": "0.52", "size": "15"}],
    }
    session = FakeSession([event, yes_book, no_book, {"data": {"amount": "103250.25"}}])
    source = PolymarketBTC15mSnapshotSource(
        event_slug="btc-updown-15m-1779089400",
        btc_price_source="coinbase",
        session=session,
    )

    payload = source.snapshot()

    assert payload["market_slug"] == "btc-updown-15m-1779089400"
    assert payload["condition_id"] == "0xcondition"
    assert payload["yes_token_id"] == "111"
    assert payload["no_token_id"] == "222"
    assert payload["btc_price"] == 103_250.25
    assert payload["strike"] == 103_000.0
    assert payload["yes_bid"] == 0.49
    assert payload["yes_ask"] == 0.51
    assert payload["no_bid"] == 0.48
    assert payload["no_ask"] == 0.52
    assert str(session.gets[0]["url"]).endswith("/events/slug/btc-updown-15m-1779089400")
    assert session.gets[1]["params"] == {"token_id": "111"}
    assert session.gets[2]["params"] == {"token_id": "222"}


def test_snapshot_row_allows_missing_strike_when_unavailable() -> None:
    row = snapshot_row_from_payload(_payload(strike=None, target_price=None))

    assert row["strike"] is None
    assert row["target_price"] is None
    assert row["distance_from_strike"] is None


def test_market_strike_ignores_zero_group_threshold_for_updown_markets() -> None:
    session = FakeSession(
        [
            {
                "slug": "btc-updown-15m-1779089400",
                "markets": [
                    {
                        "slug": "btc-updown-15m-1779089400",
                        "conditionId": "0xcondition",
                        "clobTokenIds": '["111", "222"]',
                        "outcomes": '["Up", "Down"]',
                        "groupItemThreshold": "0",
                        "startDate": "2026-05-18T04:45:00Z",
                        "endDate": "2999-05-18T05:00:00Z",
                        "question": "Bitcoin Up or Down - May 18, 3:30AM-3:45AM ET",
                    }
                ],
            },
            {"market": "0xcondition", "asset_id": "111", "bids": [], "asks": []},
            {"market": "0xcondition", "asset_id": "222", "bids": [], "asks": []},
            {"data": {"amount": "103250.25"}},
        ]
    )
    source = PolymarketBTC15mSnapshotSource(event_slug="btc-updown-15m-1779089400", btc_price_source="coinbase", session=session)

    payload = source.snapshot()

    assert payload["strike"] is None


def test_snapshot_source_can_use_binance_us_price_feed() -> None:
    session = FakeSession([{"symbol": "BTCUSDT", "price": "103333.12"}])
    source = PolymarketBTC15mSnapshotSource(btc_price_source="binance_us", session=session)

    price, raw = source._btc_price()

    assert price == 103_333.12
    assert raw["source"] == "binance_us"
    assert "binance.us" in str(session.gets[0]["url"])


def test_snapshot_source_uses_chainlink_current_and_open_reference(tmp_path: Path) -> None:
    store = ChainlinkPriceStore(tmp_path / "poly.sqlite3")
    store.record_tick(
        ChainlinkPriceTick(
            ts=datetime.fromtimestamp(1779089400.0, tz=UTC),
            price=76_983.74,
            raw={"kind": "open"},
        )
    )
    store.record_tick(
        ChainlinkPriceTick(
            ts=datetime.now(UTC),
            price=76_960.0,
            raw={"kind": "latest"},
        )
    )
    event = {
        "slug": "btc-updown-15m-1779089400",
        "markets": [
            {
                "slug": "btc-updown-15m-1779089400",
                "conditionId": "0xcondition",
                "clobTokenIds": '["111", "222"]',
                "outcomes": '["Up", "Down"]',
                "endDate": "2999-05-18T05:00:00Z",
                "question": "Bitcoin Up or Down",
            }
        ],
    }
    session = FakeSession(
        [
            event,
            {"market": "0xcondition", "asset_id": "111", "bids": [], "asks": []},
            {"market": "0xcondition", "asset_id": "222", "bids": [], "asks": []},
        ]
    )
    source = PolymarketBTC15mSnapshotSource(
        event_slug="btc-updown-15m-1779089400",
        btc_price_source="chainlink_rtds",
        chainlink_store=store,
        session=session,
    )

    payload = source.snapshot()

    assert payload["btc_price"] == 76_960.0
    assert payload["strike"] == 76_983.74
    assert payload["target_price"] == 76_983.74
    assert payload["market_open_time"] == datetime.fromtimestamp(1779089400, tz=UTC)
    assert payload["reference_price_raw"]["source"] == "polymarket_chainlink_rtds_open_reference"
