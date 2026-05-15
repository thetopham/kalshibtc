from __future__ import annotations

from kalshi_btc_15m_bot.cli import run_loop


class FakeBot:
    def __init__(self) -> None:
        self.calls = 0

    def scan_once(self) -> dict[str, object]:
        self.calls += 1
        return {
            "market_ticker": f"KXBTC15M-TEST-{self.calls}",
            "market_close_time": "2026-01-04T00:10:00+00:00",
            "target_price": 100_000.0,
            "current_price": 100_100.0,
            "probability_yes": 0.57,
            "probability_no": 0.43,
            "action": "HOLD",
            "side": None,
            "edge": 0.01,
            "stake_dollars": 0.0,
            "paper_trade_id": None,
            "paper_account": {"cash": 1000.0, "open_notional": 0.0, "realized_pnl": 0.0},
            "reasons": ["test scan"],
        }


def test_run_loop_respects_max_scans_and_sleeps_between_scans() -> None:
    bot = FakeBot()
    emitted: list[str] = []
    sleeps: list[float] = []

    exit_code = run_loop(
        bot,
        max_scans=2,
        interval_seconds=0.25,
        json_output=False,
        sleep=sleeps.append,
        emit=emitted.append,
    )

    assert exit_code == 0
    assert bot.calls == 2
    assert sleeps == [0.25]
    assert len(emitted) == 2
    assert "BTC 15m Kalshi paper scan" in emitted[0]
    assert "KXBTC15M-TEST-2" in emitted[1]


class FlakyBot(FakeBot):
    def scan_once(self) -> dict[str, object]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary provider outage")
        return {
            "market_ticker": "KXBTC15M-RECOVERED",
            "market_close_time": "2026-01-04T00:10:00+00:00",
            "target_price": 100_000.0,
            "current_price": 100_100.0,
            "probability_yes": 0.57,
            "probability_no": 0.43,
            "action": "HOLD",
            "side": None,
            "edge": 0.01,
            "stake_dollars": 0.0,
            "paper_trade_id": None,
            "paper_account": {"cash": 1000.0, "open_notional": 0.0, "realized_pnl": 0.0},
            "reasons": ["recovered scan"],
        }


def test_run_loop_continues_after_transient_scan_error() -> None:
    bot = FlakyBot()
    emitted: list[str] = []
    sleeps: list[float] = []

    exit_code = run_loop(
        bot,
        max_scans=1,
        interval_seconds=60.0,
        json_output=False,
        sleep=sleeps.append,
        emit=emitted.append,
        max_consecutive_errors=3,
        error_sleep_seconds=0.5,
    )

    assert exit_code == 0
    assert bot.calls == 2
    assert sleeps == [0.5]
    assert "scan_error: temporary provider outage" in emitted[0]
    assert "KXBTC15M-RECOVERED" in emitted[1]
