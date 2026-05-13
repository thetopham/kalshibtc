from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from typing import Any

from .backtest import run_backtest_from_provider
from .bot import (
    KalshiBTC15MBot,
    dump_json,
    format_live_auth_check,
    format_report,
    format_scan,
    format_status,
)
from .config import load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kbtc15",
        description="Paper-first BTC 15m Kalshi prediction bot",
    )
    parser.add_argument("--config", help="Path to TOML config", default=None)
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", help="Run one public-data prediction scan and optional paper trade")
    run = sub.add_parser("run", help="Continuously run paper scans until stopped")
    run.add_argument(
        "--interval-seconds",
        type=float,
        default=60.0,
        help="Seconds to sleep between scans (default: 60)",
    )
    run.add_argument(
        "--max-scans",
        type=int,
        default=None,
        help="Stop after N scans; useful for smoke tests",
    )
    run.add_argument(
        "--max-consecutive-errors",
        type=int,
        default=5,
        help="Exit non-zero after this many consecutive scan errors (default: 5)",
    )
    run.add_argument(
        "--error-sleep-seconds",
        type=float,
        default=10.0,
        help="Seconds to sleep after a failed scan before retrying (default: 10)",
    )
    sub.add_parser("status", help="Show local paper/live ledger status")
    sub.add_parser("report", help="Show operator-grade paper/live performance report")
    sub.add_parser(
        "auth-check",
        help="Run authenticated read-only Kalshi balance/position check; submits no orders",
    )
    sub.add_parser("live-status", help="Show local live order/fill ledger without submitting orders")
    sub.add_parser(
        "sync-live-fills",
        help="Fetch recent authenticated Kalshi fills into the live ledger; submits no orders",
    )
    sub.add_parser("resolve", help="Try to settle open paper trades from Kalshi market results")
    sub.add_parser("markets", help="List current KXBTC15M markets from Kalshi")
    backtest = sub.add_parser("backtest", help="Run offline BTC 15m directional backtest")
    backtest.add_argument("--show-trades", action="store_true", help="Include synthetic trades in JSON")
    return parser


def run_loop(
    bot: KalshiBTC15MBot,
    *,
    max_scans: int | None,
    interval_seconds: float,
    json_output: bool,
    max_consecutive_errors: int = 5,
    error_sleep_seconds: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] | None = None,
) -> int:
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be greater than zero")
    if error_sleep_seconds <= 0:
        raise ValueError("error_sleep_seconds must be greater than zero")
    if max_scans is not None and max_scans < 1:
        raise ValueError("max_scans must be at least 1 when provided")
    if max_consecutive_errors < 1:
        raise ValueError("max_consecutive_errors must be at least 1")

    scans = 0
    consecutive_errors = 0

    def write(text: str) -> None:
        if emit is not None:
            emit(text)
        else:
            print(text, flush=True)

    while max_scans is None or scans < max_scans:
        try:
            payload = bot.scan_once()
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 - long-running bot should survive transient providers.
            consecutive_errors += 1
            error_payload = {
                "event": "scan_error",
                "error": str(exc),
                "consecutive_errors": consecutive_errors,
                "max_consecutive_errors": max_consecutive_errors,
            }
            write(dump_json(error_payload) if json_output else f"scan_error: {exc}")
            if consecutive_errors >= max_consecutive_errors:
                return 1
            sleep(error_sleep_seconds)
            continue

        consecutive_errors = 0
        write(dump_json(payload) if json_output else format_scan(payload))
        scans += 1
        if max_scans is not None and scans >= max_scans:
            break
        sleep(interval_seconds)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        bot = KalshiBTC15MBot(config)

        if args.command == "scan":
            payload = bot.scan_once()
            print(dump_json(payload) if args.json else format_scan(payload))
            return 0

        if args.command == "run":
            return run_loop(
                bot,
                max_scans=args.max_scans,
                interval_seconds=args.interval_seconds,
                json_output=args.json,
                max_consecutive_errors=args.max_consecutive_errors,
                error_sleep_seconds=args.error_sleep_seconds,
            )

        if args.command == "status":
            payload = bot.status()
            print(dump_json(payload) if args.json else format_status(payload))
            return 0

        if args.command == "report":
            payload = bot.report()
            print(dump_json(payload) if args.json else format_report(payload))
            return 0

        if args.command == "auth-check":
            payload = bot.live_auth_check()
            print(dump_json(payload) if args.json else format_live_auth_check(payload))
            return 0

        if args.command == "live-status":
            payload = bot.live_status(sync_fills=False)
            print(dump_json(payload))
            return 0

        if args.command == "sync-live-fills":
            payload = bot.sync_live_fills()
            print(dump_json(payload))
            return 0

        if args.command == "resolve":
            settled = bot.resolve_open_trades()
            payload = {"settled_trades": settled, "status": bot.status()}
            print(dump_json(payload) if args.json else f"settled_trades: {settled}")
            return 0

        if args.command == "markets":
            payload = bot.markets()
            if args.json:
                print(dump_json(payload))
            else:
                for market in payload:
                    print(
                        f"{market['ticker']} close={market.get('close_time')} "
                        f"target={market.get('target_price')} yes={market.get('yes_bid')}/{market.get('yes_ask')} "
                        f"no={market.get('no_bid')}/{market.get('no_ask')} status={market.get('status')}"
                    )
            return 0

        if args.command == "backtest":
            result = run_backtest_from_provider(config)
            payload: dict[str, Any] = {"metrics": result.metrics}
            if args.show_trades:
                payload["trades"] = result.trades
            if args.json:
                print(dump_json(payload))
            else:
                m = result.metrics
                print("BTC 15m directional backtest")
                print(f"samples: {m['samples']} train: {m['train_samples']} test: {m['test_samples']}")
                print(f"accuracy: {m['test_accuracy']:.3f} brier: {m['test_brier']:.3f} baseline_up_rate: {m['baseline_up_rate']:.3f}")
                print(f"trades: {m['trade_count']} win_rate: {m['trade_win_rate']:.3f} synthetic_pnl: {m['synthetic_total_pnl_per_1usd_trades']:.2f}")
                print(m["note"])
            return 0

        parser.error(f"Unknown command: {args.command}")
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI should print concise operator errors.
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
