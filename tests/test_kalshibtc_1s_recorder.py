from __future__ import annotations

import ast
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from kalshibtc.record_1s_snapshots import (
    PublicRestSnapshotSource,
    RealtimeSnapshotRecorder,
    snapshot_row_from_payload,
)
from kalshibtc.storage.paper_signal_store import PaperSignalStore, initialize_results_db

BASE_TS = datetime(2026, 5, 15, 12, 0, 3, 123456, tzinfo=UTC)
CLOSE_TS = datetime(2026, 5, 15, 12, 15, tzinfo=UTC)
OPEN_TS = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "ts": BASE_TS.isoformat(),
        "market_ticker": "KXBTC15M-TEST",
        "market_open_time": OPEN_TS.isoformat(),
        "market_close_time": CLOSE_TS.isoformat(),
        "btc_price": 100_025.25,
        "strike": 100_000.0,
        "target_price": 100_000.0,
        "btc_velocity_30s": 1.25,
        "yes_bid": 0.51,
        "yes_ask": 0.53,
        "no_bid": 0.46,
        "no_ask": 0.48,
        "orderbook_sequence": 42,
    }
    payload.update(overrides)
    return payload


def _row(db_path: Path) -> sqlite3.Row:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM realtime_snapshots_1s").fetchone()


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.payload


class FakeSession:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.headers: dict[str, str] = {}
        self.posts: list[dict[str, object]] = []

    def get(self, *_args: object, **_kwargs: object) -> FakeResponse:
        return FakeResponse(self.responses.pop(0))

    def post(self, *_args: object, **kwargs: object) -> FakeResponse:
        self.posts.append(kwargs)
        return FakeResponse(self.responses.pop(0))


def test_recorder_initializes_realtime_snapshot_table(tmp_path: Path) -> None:
    db_path = tmp_path / "realtime-snapshots-1s.sqlite3"

    RealtimeSnapshotRecorder(db_path)

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(realtime_snapshots_1s)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(realtime_snapshots_1s)")}

    assert {
        "ts",
        "market_ticker",
        "market_open_time",
        "market_close_time",
        "btc_price",
        "strike",
        "target_price",
        "seconds_to_close",
        "btc_velocity_30s",
        "slope_30s",
        "yes_bid",
        "yes_ask",
        "no_bid",
        "no_ask",
        "orderbook_sequence",
        "raw_json",
    } <= columns
    assert "idx_realtime_snapshots_1s_ts" in indexes
    assert "idx_realtime_snapshots_1s_market_close" in indexes


def test_recorder_writes_one_compatible_fake_snapshot_row(tmp_path: Path) -> None:
    db_path = tmp_path / "realtime-snapshots-1s.sqlite3"
    recorder = RealtimeSnapshotRecorder(db_path)

    recorded = recorder.record_snapshot(_payload())

    assert recorded is True
    row = _row(db_path)
    assert row["ts"] == "2026-05-15T12:00:03+00:00"
    assert row["market_ticker"] == "KXBTC15M-TEST"
    assert row["btc_price"] == 100_025.25
    assert row["strike"] == 100_000.0
    assert row["target_price"] == 100_000.0
    assert row["seconds_to_close"] == 896.877
    assert row["btc_velocity_30s"] == 1.25
    assert row["slope_30s"] == 1.25
    assert row["yes_bid"] == 0.51
    assert row["yes_ask"] == 0.53
    assert row["no_bid"] == 0.46
    assert row["no_ask"] == 0.48
    assert row["orderbook_sequence"] == 42
    assert "KXBTC15M-TEST" in row["raw_json"]


def test_duplicate_same_second_snapshot_upserts_instead_of_duplicating(tmp_path: Path) -> None:
    db_path = tmp_path / "realtime-snapshots-1s.sqlite3"
    recorder = RealtimeSnapshotRecorder(db_path)

    recorder.record_snapshot(_payload(btc_price=100_001.0, yes_ask=0.52))
    recorder.record_snapshot(
        _payload(
            ts="2026-05-15T12:00:03.900000+00:00",
            btc_price=100_009.0,
            yes_ask=0.55,
            orderbook_sequence=43,
        )
    )

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        count = conn.execute("SELECT COUNT(*) FROM realtime_snapshots_1s").fetchone()[0]
        row = conn.execute("SELECT * FROM realtime_snapshots_1s").fetchone()

    assert count == 1
    assert row["btc_price"] == 100_009.0
    assert row["yes_ask"] == 0.55
    assert row["orderbook_sequence"] == 43


def test_paper_signal_store_can_read_recorder_snapshot(tmp_path: Path) -> None:
    snapshot_db = tmp_path / "realtime-snapshots-1s.sqlite3"
    results_db = tmp_path / "paper-results-1s.sqlite3"
    recorder = RealtimeSnapshotRecorder(snapshot_db)
    recorder.record_snapshot(_payload())
    initialize_results_db(results_db)

    with PaperSignalStore(snapshot_db=snapshot_db, results_db=results_db) as store:
        rows = store.select_unprocessed_snapshots(limit=10)
        state = store.state_from_snapshot(rows[0])

    assert state.tick.ts == datetime(2026, 5, 15, 12, 0, 3, tzinfo=UTC)
    assert state.contract.ticker == "KXBTC15M-TEST"
    assert state.price == 100_025.25
    assert state.strike == 100_000.0
    assert state.slope_30s == 1.25
    assert state.orderbook.yes_ask == 0.53
    assert state.orderbook.no_ask == 0.48


def test_snapshot_row_accepts_slope_30s_alias() -> None:
    row = snapshot_row_from_payload(_payload(btc_velocity_30s=None, slope_30s=-0.75))

    assert row["btc_velocity_30s"] == -0.75
    assert row["slope_30s"] == -0.75


def test_public_snapshot_source_treats_one_cent_market_prices_as_cents() -> None:
    source = PublicRestSnapshotSource(
        market_ticker="KXBTC15M-TEST",
        session=FakeSession(
            [
                {"data": {"amount": "100020.00"}},
                {
                    "market": {
                        "ticker": "KXBTC15M-TEST",
                        "open_time": "2026-05-15T12:00:00Z",
                        "close_time": "2999-05-15T12:15:00Z",
                        "floor_strike": "100000",
                        "yes_bid": 1,
                        "yes_ask": 99,
                        "no_bid": 2,
                        "no_ask": 98,
                    }
                },
                {"orderbook": {}},
            ]
        ),
    )

    payload = source.snapshot()

    assert payload["yes_bid"] == 0.01
    assert payload["yes_ask"] == 0.99
    assert payload["no_bid"] == 0.02
    assert payload["no_ask"] == 0.98


def test_public_snapshot_source_treats_one_cent_orderbook_levels_as_cents() -> None:
    source = PublicRestSnapshotSource(
        market_ticker="KXBTC15M-TEST",
        session=FakeSession(
            [
                {"data": {"amount": "100020.00"}},
                {
                    "market": {
                        "ticker": "KXBTC15M-TEST",
                        "open_time": "2026-05-15T12:00:00Z",
                        "close_time": "2999-05-15T12:15:00Z",
                        "floor_strike": "100000",
                    }
                },
                {"orderbook": {"yes": [[1, 10]], "no": [[2, 5]]}, "sequence": 123},
            ]
        ),
    )

    payload = source.snapshot()

    assert payload["yes_bid"] == 0.01
    assert payload["no_bid"] == 0.02
    assert payload["yes_ask"] == 0.98
    assert payload["no_ask"] == 0.99
    assert payload["orderbook_sequence"] == 123


def _uint256_hex(value: int) -> str:
    return "0x" + f"{value:064x}"


def _latest_round_data_hex(*, answer: int, updated_at: int = 1_768_506_300) -> str:
    words = [1, answer, updated_at - 5, updated_at, 1]
    return "0x" + "".join(f"{word:064x}" for word in words)


def test_public_snapshot_source_can_read_chainlink_btc_usd_price_feed() -> None:
    session = FakeSession(
        [
            {"result": _uint256_hex(8)},
            {"result": _latest_round_data_hex(answer=10_002_025_000_000)},
            {
                "market": {
                    "ticker": "KXBTC15M-TEST",
                    "open_time": "2026-05-15T12:00:00Z",
                    "close_time": "2999-05-15T12:15:00Z",
                    "floor_strike": "100000",
                    "yes_bid": 51,
                    "yes_ask": 53,
                    "no_bid": 46,
                    "no_ask": 48,
                }
            },
            {"orderbook": {}},
        ]
    )
    source = PublicRestSnapshotSource(
        market_ticker="KXBTC15M-TEST",
        price_source="chainlink",
        chainlink_rpc_url="https://example-rpc.invalid",
        session=session,
    )

    payload = source.snapshot()

    assert payload["btc_price"] == 100_020.25
    assert payload["btc_price_source"] == "chainlink"
    assert payload["btc_price_raw"]["feed_address"] == "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c"
    assert len(session.posts) == 2
    first_post = session.posts[0]["json"]
    second_post = session.posts[1]["json"]
    assert isinstance(first_post, dict)
    assert isinstance(second_post, dict)
    assert first_post["params"][0]["data"] == "0x313ce567"
    assert second_post["params"][0]["data"] == "0xfeaf968c"


def test_recorder_can_write_existing_legacy_shape_stream_table(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy-shaped-snapshots.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE realtime_snapshots_1s (
                ts TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                market_open_time TEXT,
                market_close_time TEXT,
                btc_price REAL,
                strike REAL,
                target_price REAL,
                btc_velocity_30s REAL,
                slope_30s REAL,
                yes_bid REAL,
                yes_ask REAL,
                no_bid REAL,
                no_ask REAL,
                orderbook_sequence INTEGER,
                execution_blocked_by_json TEXT NOT NULL,
                raw_state_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (market_ticker, ts)
            )
            """
        )

    recorder = RealtimeSnapshotRecorder(db_path)
    recorder.record_snapshot(_payload())

    row = _row(db_path)
    assert row["raw_json"] is not None
    assert row["raw_state_json"] is not None
    assert row["seconds_to_close"] == 896.877
    assert row["execution_blocked_by_json"] == "[]"


def test_active_kalshibtc_package_does_not_import_legacy_package() -> None:
    src_root = Path(__file__).resolve().parents[1] / "src" / "kalshibtc"
    offenders: list[str] = []
    for path in src_root.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "kalshi_btc_15m_bot" or alias.name.startswith(
                        "kalshi_btc_15m_bot."
                    ):
                        offenders.append(str(path.relative_to(src_root)))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "kalshi_btc_15m_bot" or module.startswith("kalshi_btc_15m_bot."):
                    offenders.append(str(path.relative_to(src_root)))

    assert offenders == []
