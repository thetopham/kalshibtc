from __future__ import annotations

import argparse
import json
from pathlib import Path

from .dataset import export_probability_dataset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kbtc-probability-dataset",
        description="Export per-snapshot probability labels/features from a BTC 15m feed DB.",
    )
    parser.add_argument("--feed-db", type=Path, required=True)
    parser.add_argument("--venue", required=True, choices=["kalshi", "polymarket"])
    parser.add_argument("--from", dest="from_ts", default=None)
    parser.add_argument("--to", dest="to_ts", default=None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--format", choices=["csv", "parquet"], default=None)
    parser.add_argument("--json", action="store_true", help="Print JSON summary")
    args = parser.parse_args(argv)

    summary = export_probability_dataset(
        feed_db=args.feed_db,
        venue=args.venue,
        from_ts=args.from_ts,
        to_ts=args.to_ts,
        output_path=args.output,
        output_format=args.format,
    )
    payload = {
        "rows": summary.rows,
        "markets": summary.markets,
        "output_path": str(summary.output_path),
        "format": summary.format,
    }
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(
            "kbtc_probability_dataset "
            f"rows={summary.rows} markets={summary.markets} format={summary.format} output={summary.output_path}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
