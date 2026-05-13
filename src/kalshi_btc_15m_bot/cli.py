from __future__ import annotations

import argparse
import sys
from typing import Any

from .backtest import run_backtest_from_provider
from .bot import KalshiBTC15MBot, dump_json, format_scan, format_status
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
    sub.add_parser("status", help="Show local paper ledger status")
    sub.add_parser("resolve", help="Try to settle open paper trades from Kalshi market results")
    sub.add_parser("markets", help="List current KXBTC15M markets from Kalshi")
    backtest = sub.add_parser("backtest", help="Run offline BTC 15m directional backtest")
    backtest.add_argument("--show-trades", action="store_true", help="Include synthetic trades in JSON")
    return parser


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

        if args.command == "status":
            payload = bot.status()
            print(dump_json(payload) if args.json else format_status(payload))
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
