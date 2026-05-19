from __future__ import annotations

import json
from pathlib import Path

from kalshibtc.research.journal import collect_run_summaries


def _write_minimal_run(runs_dir: Path, *, strategy: str, run_id: str, feed_db: str) -> None:
    run_dir = runs_dir / strategy / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "config.toml").write_text(
        "\n".join(
            [
                f'feed_db = "{feed_db}"',
                f'strategy = "{strategy}"',
                f'run_id = "{run_id}"',
                "strategy_params = {}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (run_dir / "metrics.json").write_text(
        json.dumps(
            {
                "strategy": strategy,
                "run_id": run_id,
                "run_dir": str(run_dir),
                "fills": 0,
                "notional": 0,
                "portfolio_settlement": {"realized_pnl": 0},
                "institutional_metrics": {},
            }
        ),
        encoding="utf-8",
    )


def test_collect_run_summaries_labels_polymarket_datafeed_from_feed_db_path(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    _write_minimal_run(
        runs_dir,
        strategy="poly_seed_repair",
        run_id="poly-run",
        feed_db="feed/polymarket-btc-1s.sqlite3",
    )

    summaries = collect_run_summaries(runs_dir)

    assert summaries[0].exchange == "polymarket"
    assert summaries[0].datafeed == "polymarket-btc-1s"
