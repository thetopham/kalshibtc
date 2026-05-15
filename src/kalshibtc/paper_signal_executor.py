from __future__ import annotations

import argparse
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import RiskLimits
from .execution.paper import PaperExecutor, PaperFill
from .execution.risk import RiskManager
from .market.kalshi_public import KalshiPublicClient
from .market.state import MarketState
from .storage.paper_signal_store import (
    PaperSignalStore,
    SettlementDecision,
    action_and_side,
    count_paper_trades,
    initialize_results_db,
)
from .strategy.signals import Strategy
from .strategy.simple_directional import SimpleDirectionalStrategy

KALSHI_PUBLIC_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"

__all__ = [
    "OneSecondPaperTrader",
    "PaperRunSummary",
    "count_paper_trades",
    "initialize_results_db",
    "main",
]


@dataclass(frozen=True)
class PaperRunSummary:
    snapshots_processed: int = 0
    signals_recorded: int = 0
    trades_opened: int = 0
    trades_closed: int = 0
    skipped_snapshots: int = 0


class OneSecondPaperTrader:
    """Paper-only executor for the 1s websocket tape.

    The source snapshot database is recorder-owned. This class only reads rows
    from it and writes all generated signals, fake fills, exits, offsets, and
    PnL into a separate results database.
    """

    def __init__(
        self,
        *,
        snapshot_db: str | Path,
        ledger_db: str | Path,
        strategies: Sequence[Strategy] | None = None,
        risk_limits: RiskLimits | None = None,
        paper_executor: PaperExecutor | None = None,
        official_settlement_client: Any | None = None,
    ) -> None:
        self.snapshot_db = Path(snapshot_db)
        self.ledger_db = Path(ledger_db)
        if self.snapshot_db.resolve() == self.ledger_db.resolve():
            raise ValueError("snapshot_db and ledger_db must be separate databases")
        self.strategies = list(strategies or [SimpleDirectionalStrategy()])
        self.risk_manager = RiskManager(risk_limits or RiskLimits())
        self.paper_executor = paper_executor or PaperExecutor()
        self.official_settlement_client = official_settlement_client
        initialize_results_db(self.ledger_db)

    def run_once(self, *, limit: int = 250) -> PaperRunSummary:
        processed = signals = opened = closed = skipped = 0
        official_cache: dict[str, SettlementDecision | None] = {}
        with PaperSignalStore(snapshot_db=self.snapshot_db, results_db=self.ledger_db) as store:
            rows = store.select_unprocessed_snapshots(limit=limit)
            for row in rows:
                snapshot_key = store.snapshot_key(row)
                if store.is_snapshot_processed(snapshot_key):
                    store.advance_cursor(row)
                    skipped += 1
                    continue
                try:
                    state = store.state_from_snapshot(row)
                except (KeyError, TypeError, ValueError):
                    store.mark_snapshot_processed(snapshot_key)
                    store.advance_cursor(row)
                    skipped += 1
                    continue

                processed += 1
                store.upgrade_estimated_settlements(
                    self.official_settlement_client,
                    official_cache=official_cache,
                )
                closed += store.settle_expired_positions(
                    state,
                    official_client=self.official_settlement_client,
                    official_cache=official_cache,
                )
                closed += store.settle_expired_positions_from_stream(
                    state.tick.ts,
                    official_client=self.official_settlement_client,
                    official_cache=official_cache,
                )

                open_positions = store.open_position_count()
                for strategy in self.strategies:
                    signal = strategy.on_tick(state)
                    risk = self.risk_manager.evaluate(
                        state,
                        signal,
                        open_positions=open_positions,
                    )
                    action, paper_side = action_and_side(signal)
                    prediction_id = store.prediction_id(state, signal.strategy)
                    store.record_prediction(
                        prediction_id=prediction_id,
                        state=state,
                        signal=signal,
                        risk=risk,
                        action=action,
                        paper_side=paper_side,
                    )
                    signals += 1

                    fill = self.paper_executor.execute(state, risk)
                    if _can_open_trade(state, fill, paper_side):
                        trade_opened = store.open_trade(
                            prediction_id=prediction_id,
                            state=state,
                            signal=signal,
                            paper_side=paper_side,
                            fill=fill,
                        )
                        if trade_opened:
                            opened += 1
                            open_positions += 1

                closed += store.settle_expired_positions(
                    state,
                    official_client=self.official_settlement_client,
                    official_cache=official_cache,
                )
                store.mark_snapshot_processed(snapshot_key)
                store.advance_cursor(row)

        return PaperRunSummary(
            snapshots_processed=processed,
            signals_recorded=signals,
            trades_opened=opened,
            trades_closed=closed,
            skipped_snapshots=skipped,
        )


def _can_open_trade(state: MarketState, fill: PaperFill | None, paper_side: str | None) -> bool:
    return bool(paper_side is not None and fill is not None and state.seconds_to_close > 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m kalshibtc.paper_signal_executor",
        description="Paper-trade the 1s Kalshi BTC snapshot tape into a separate results database.",
    )
    parser.add_argument(
        "--snapshot-db",
        default="data/realtime-snapshots-1s.sqlite3",
        help="Recorder-owned 1s stream database to SELECT from; this command does not write to it.",
    )
    parser.add_argument(
        "--results-db",
        default="data-live-prod/paper-results-1s.sqlite3",
        help="Separate writable results database for signals, fake fills, exits, and PnL.",
    )
    parser.add_argument(
        "--limit", type=int, default=250, help="Max new snapshots to process per pass."
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Continuously poll the snapshot DB and write paper results until stopped.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=1.0,
        help="Sleep interval between loop passes when --loop is set.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON summaries.")
    parser.add_argument(
        "--kalshi-base-url",
        default=KALSHI_PUBLIC_BASE_URL,
        help="Kalshi public REST base URL used only to read official market results for settlement.",
    )
    parser.add_argument(
        "--official-settlement-timeout-seconds",
        type=int,
        default=10,
        help="Timeout for read-only Kalshi official settlement lookups.",
    )
    parser.add_argument(
        "--no-official-settlement",
        action="store_true",
        help="Disable Kalshi official result lookups and settle from Coinbase/raw 1s estimates only.",
    )
    args = parser.parse_args(argv)

    official_settlement_client = None
    if not args.no_official_settlement:
        official_settlement_client = KalshiPublicClient(
            base_url=args.kalshi_base_url,
            timeout_seconds=args.official_settlement_timeout_seconds,
        )

    trader = OneSecondPaperTrader(
        snapshot_db=Path(args.snapshot_db),
        ledger_db=Path(args.results_db),
        official_settlement_client=official_settlement_client,
    )

    def emit(summary: PaperRunSummary) -> None:
        data = summary.__dict__
        if args.json:
            print(json.dumps(data, sort_keys=True), flush=True)
        else:
            print(
                "1s_paper "
                f"processed={summary.snapshots_processed} "
                f"signals={summary.signals_recorded} "
                f"opened={summary.trades_opened} "
                f"closed={summary.trades_closed} "
                f"skipped={summary.skipped_snapshots}",
                flush=True,
            )

    while True:
        emit(trader.run_once(limit=args.limit))
        if not args.loop:
            return 0
        time.sleep(max(0.1, float(args.interval_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
