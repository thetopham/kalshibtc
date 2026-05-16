from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import RiskLimits
from .market.kalshi_public import KalshiPublicClient
from .paper_signal_executor import KALSHI_PUBLIC_BASE_URL, OneSecondPaperTrader, PaperRunSummary
from .runtime_paths import resolve_feed_db, resolve_runs_dir
from .strategy.registry import create_strategy


@dataclass(frozen=True)
class MultiStrategyPaperRunSummary:
    mode: str
    snapshots_db_path: str
    runs_dir_path: str
    strategies: list[dict[str, Any]]


def run_live_strategy_paper_once(
    *,
    snapshot_db: str | Path | None = None,
    runs_dir: str | Path | None = None,
    strategies: list[str] | tuple[str, ...] | None = None,
    limit: int = 250,
    risk_limits: RiskLimits | None = None,
    official_settlement_client: Any | None = None,
    no_official_settlement: bool = False,
    kalshi_base_url: str = KALSHI_PUBLIC_BASE_URL,
    official_settlement_timeout_seconds: int = 10,
) -> MultiStrategyPaperRunSummary:
    snapshot_path = resolve_feed_db(snapshot_db)
    runs_path = resolve_runs_dir(runs_dir)
    names = list(strategies or ["simple_directional"])
    official_client = official_settlement_client
    if official_client is None and not no_official_settlement:
        official_client = KalshiPublicClient(
            base_url=kalshi_base_url,
            timeout_seconds=official_settlement_timeout_seconds,
        )

    summaries: list[dict[str, Any]] = []
    for name in names:
        run_dir = runs_path / "live" / name
        run_dir.mkdir(parents=True, exist_ok=True)
        results_db = run_dir / "results.sqlite3"
        strategy = create_strategy(name)
        trader = OneSecondPaperTrader(
            snapshot_db=snapshot_path,
            ledger_db=results_db,
            strategies=[strategy],
            risk_limits=risk_limits,
            official_settlement_client=official_client,
        )
        summary = trader.run_once(limit=limit)
        payload = _metrics_payload(
            strategy=name,
            summary=summary,
            snapshot_db=snapshot_path,
            run_dir=run_dir,
            results_db=results_db,
        )
        _write_live_config(run_dir / "config.toml", strategy=name, snapshot_db=snapshot_path, results_db=results_db)
        (run_dir / "metrics.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        summaries.append(payload)

    return MultiStrategyPaperRunSummary(
        mode="live_paper",
        snapshots_db_path=str(snapshot_path),
        runs_dir_path=str(runs_path),
        strategies=summaries,
    )


def parse_strategy_list(value: str | None) -> list[str]:
    if not value:
        return ["simple_directional"]
    return [part.strip() for part in value.split(",") if part.strip()]


def _metrics_payload(
    *,
    strategy: str,
    summary: PaperRunSummary,
    snapshot_db: Path,
    run_dir: Path,
    results_db: Path,
) -> dict[str, Any]:
    return {
        "mode": "live_paper",
        "strategy": strategy,
        "run_id": "live",
        "snapshots_db": str(snapshot_db),
        "run_dir": str(run_dir),
        "results_db": str(results_db),
        "snapshots_processed": summary.snapshots_processed,
        "signals_recorded": summary.signals_recorded,
        "trades_opened": summary.trades_opened,
        "trades_closed": summary.trades_closed,
        "skipped_snapshots": summary.skipped_snapshots,
        # Reuse comparison field names from replay dashboard.
        "snapshots": summary.snapshots_processed,
        "signals": summary.signals_recorded,
        "fills": summary.trades_opened,
    }


def _write_live_config(path: Path, *, strategy: str, snapshot_db: Path, results_db: Path) -> None:
    path.write_text(
        "\n".join(
            [
                'mode = "live_paper"',
                f'strategy = "{strategy}"',
                'run_id = "live"',
                f'snapshot_db = "{snapshot_db}"',
                f'results_db = "{results_db}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
