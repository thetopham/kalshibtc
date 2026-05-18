from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from kalshibtc.polymarket_chainlink import (
    ChainlinkPriceStore,
    ChainlinkPriceTick,
    parse_chainlink_rtds_message,
)


def _message(value: float = 76983.74, ts_ms: int = 1779091200123) -> str:
    return (
        '{"topic":"crypto_prices_chainlink","type":"update","timestamp":1779091200456,'
        f'"payload":{{"symbol":"btc/usd","timestamp":{ts_ms},"value":{value}}}}}'
    )


def test_parse_chainlink_rtds_btc_message() -> None:
    ticks = parse_chainlink_rtds_message(_message())

    assert ticks
    tick = ticks[0]
    assert tick.price == 76983.74
    assert tick.ts == datetime.fromtimestamp(1779091200.123, tz=UTC)
    assert tick.raw["topic"] == "crypto_prices_chainlink"


def test_chainlink_store_records_latest_tick_and_market_reference(tmp_path: Path) -> None:
    db_path = tmp_path / "poly.sqlite3"
    store = ChainlinkPriceStore(db_path)
    tick = ChainlinkPriceTick(
        ts=datetime(2026, 5, 18, 8, 0, 0, 123000, tzinfo=UTC),
        price=76983.74,
        raw={"source": "test"},
    )

    store.record_tick(tick)
    latest = store.latest_tick(max_age_seconds=None)
    reference = store.reference_for_market(
        market_slug="btc-updown-15m-1779091200",
        open_time=datetime(2026, 5, 18, 8, 0, 0, tzinfo=UTC),
    )

    assert latest is not None
    assert latest.price == 76983.74
    assert reference is not None
    assert reference.price == 76983.74
    assert store.cached_reference("btc-updown-15m-1779091200") is not None
