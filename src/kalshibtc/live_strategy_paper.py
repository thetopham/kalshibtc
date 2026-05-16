from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from .config import RiskLimits
from .market.kalshi_public import KalshiPublicClient
from .paper_signal_executor import KALSHI_PUBLIC_BASE_URL, OneSecondPaperTrader, PaperRunSummary
from .pair_arb_paper import PairArbPaperTrader
from .pair_arb_grid_paper import PairArbGridPaperTrader
from .pair_arb_passive_paper import PairArbPassivePaperTrader
from .inventory_vol_rebalance_paper import InventoryVolRebalancePaperTrader
from .inventory_vol_regime_paper import InventoryVolRegimePaperTrader
from .runtime_paths import resolve_feed_db, resolve_runs_dir
from .strategy.registry import create_strategy
from .volatility_hedge_paper import VolatilityHedgeConfig, VolatilityHedgePaperTrader


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
        if name == "pair_arb_passive":
            trader = PairArbPassivePaperTrader(snapshot_db=snapshot_path, results_db=results_db)
            passive_payload = trader.run_once(limit=limit)
            payload = {
                "mode": "live_paper",
                "strategy": name,
                "run_id": "live",
                "snapshots_db": str(snapshot_path),
                "run_dir": str(run_dir),
                "results_db": str(results_db),
                "snapshots_processed": passive_payload.get("snapshots_processed", 0),
                "signals_recorded": passive_payload.get("signals_recorded", 0),
                "trades_opened": passive_payload.get("passive_fills", 0),
                "trades_closed": 0,
                "skipped_snapshots": 0,
                "snapshots": passive_payload.get("snapshots_processed", 0),
                "signals": passive_payload.get("signals_recorded", 0),
                "fills": passive_payload.get("passive_fills", 0),
                **passive_payload,
            }
            _write_live_config(run_dir / "config.toml", strategy=name, snapshot_db=snapshot_path, results_db=results_db)
            (run_dir / "metrics.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            summaries.append(payload)
            continue
        if name == "pair_arb_grid":
            trader = PairArbGridPaperTrader(snapshot_db=snapshot_path, results_db=results_db)
            grid_payload = trader.run_once(limit=limit)
            payload = {
                "mode": "live_paper",
                "strategy": name,
                "run_id": "live",
                "snapshots_db": str(snapshot_path),
                "run_dir": str(run_dir),
                "results_db": str(results_db),
                "snapshots_processed": grid_payload.get("snapshots_processed", 0),
                "signals_recorded": grid_payload.get("signals_recorded", 0),
                "trades_opened": grid_payload.get("fills", 0),
                "trades_closed": 0,
                "skipped_snapshots": 0,
                "snapshots": grid_payload.get("snapshots_processed", 0),
                "signals": grid_payload.get("signals_recorded", 0),
                "fills": grid_payload.get("fills", 0),
                **grid_payload,
            }
            _write_live_config(run_dir / "config.toml", strategy=name, snapshot_db=snapshot_path, results_db=results_db)
            (run_dir / "metrics.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            summaries.append(payload)
            continue
        if name == "inventory_vol_rebalance":
            trader = InventoryVolRebalancePaperTrader(snapshot_db=snapshot_path, results_db=results_db)
            inventory_payload = trader.run_once(limit=limit)
            payload = {
                "mode": "live_paper",
                "strategy": name,
                "run_id": "live",
                "snapshots_db": str(snapshot_path),
                "run_dir": str(run_dir),
                "results_db": str(results_db),
                "snapshots_processed": inventory_payload.get("snapshots_processed", 0),
                "signals_recorded": inventory_payload.get("signals_recorded", 0),
                "trades_opened": inventory_payload.get("fills", 0),
                "trades_closed": inventory_payload.get("reductions", 0),
                "skipped_snapshots": 0,
                "snapshots": inventory_payload.get("snapshots_processed", 0),
                "signals": inventory_payload.get("signals_recorded", 0),
                "fills": inventory_payload.get("fills", 0),
                **inventory_payload,
            }
            _write_live_config(run_dir / "config.toml", strategy=name, snapshot_db=snapshot_path, results_db=results_db)
            (run_dir / "metrics.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            summaries.append(payload)
            continue
        if name == "inventory_vol_regime":
            trader = InventoryVolRegimePaperTrader(snapshot_db=snapshot_path, results_db=results_db)
            inventory_payload = trader.run_once(limit=limit)
            payload = {
                "mode": "live_paper",
                "strategy": name,
                "run_id": "live",
                "snapshots_db": str(snapshot_path),
                "run_dir": str(run_dir),
                "results_db": str(results_db),
                "snapshots_processed": inventory_payload.get("snapshots_processed", 0),
                "signals_recorded": inventory_payload.get("signals_recorded", 0),
                "trades_opened": inventory_payload.get("fills", 0),
                "trades_closed": 0,
                "skipped_snapshots": 0,
                "snapshots": inventory_payload.get("snapshots_processed", 0),
                "signals": inventory_payload.get("signals_recorded", 0),
                "fills": inventory_payload.get("fills", 0),
                **inventory_payload,
            }
            _write_live_config(run_dir / "config.toml", strategy=name, snapshot_db=snapshot_path, results_db=results_db)
            (run_dir / "metrics.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            summaries.append(payload)
            continue
        if name == "volatility_hedge":
            hedge_config = VolatilityHedgeConfig()
            trader = VolatilityHedgePaperTrader(snapshot_db=snapshot_path, results_db=results_db, config=hedge_config)
            hedge_payload = trader.run_once(limit=limit)
            payload = {
                "mode": "live_paper",
                "strategy": name,
                "run_id": "live",
                "snapshots_db": str(snapshot_path),
                "run_dir": str(run_dir),
                "results_db": str(results_db),
                "snapshots_processed": hedge_payload.get("snapshots", hedge_payload.get("snapshots_processed", 0)),
                "signals_recorded": hedge_payload.get("signals", hedge_payload.get("signals_recorded", 0)),
                "trades_opened": hedge_payload.get("fills", 0),
                "trades_closed": 0,
                "skipped_snapshots": 0,
                "snapshots": hedge_payload.get("snapshots_processed", 0),
                "signals": hedge_payload.get("signals_recorded", 0),
                "fills": hedge_payload.get("fills", 0),
                **hedge_payload,
            }
            _write_live_config(run_dir / "config.toml", strategy=name, snapshot_db=snapshot_path, results_db=results_db, volatility_hedge_config=hedge_config)
            (run_dir / "metrics.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            summaries.append(payload)
            continue
        if name == "pair_arb":
            trader = PairArbPaperTrader(snapshot_db=snapshot_path, results_db=results_db)
            pair_payload = trader.run_once(limit=limit)
            payload = {
                "mode": "live_paper",
                "strategy": name,
                "run_id": "live",
                "snapshots_db": str(snapshot_path),
                "run_dir": str(run_dir),
                "results_db": str(results_db),
                "snapshots_processed": pair_payload.get("snapshots_processed", 0),
                "signals_recorded": pair_payload.get("signals_recorded", 0),
                "trades_opened": pair_payload.get("initial_inventory_fills", 0) + pair_payload.get("hedge_fills", 0),
                "trades_closed": 0,
                "skipped_snapshots": 0,
                "snapshots": pair_payload.get("snapshots_processed", 0),
                "signals": pair_payload.get("signals_recorded", 0),
                "fills": pair_payload.get("initial_inventory_fills", 0) + pair_payload.get("hedge_fills", 0),
                **pair_payload,
            }
            _write_live_config(run_dir / "config.toml", strategy=name, snapshot_db=snapshot_path, results_db=results_db)
            (run_dir / "metrics.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            summaries.append(payload)
            continue
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
        payload.update(_cumulative_metrics(results_db))
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


def _cumulative_metrics(results_db: Path) -> dict[str, int | float]:
    with sqlite3.connect(f"file:{results_db}?mode=ro", uri=True, timeout=2.0) as conn:
        conn.execute("PRAGMA query_only=ON")
        processed = _table_count(conn, "processed_snapshots")
        predictions = _table_count(conn, "predictions")
        trades = _table_count(conn, "paper_trades")
        opened = _trade_status_count(conn, open_only=True)
        closed = _trade_status_count(conn, open_only=False)
    return {
        "snapshots_processed": processed,
        "signals_recorded": predictions,
        "trades_opened": trades,
        "trades_closed": closed,
        "open_trades": opened,
        "closed_trades": closed,
        "fills": trades,
        "snapshots": processed,
        "signals": predictions,
    }


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _trade_status_count(conn: sqlite3.Connection, *, open_only: bool) -> int:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_trades'").fetchone():
        return 0
    statuses = {str(row[0] or "").upper() for row in conn.execute("SELECT DISTINCT status FROM paper_trades")}
    if open_only:
        return int(conn.execute("SELECT COUNT(*) FROM paper_trades WHERE UPPER(COALESCE(status, '')) = 'OPEN'").fetchone()[0])
    if not statuses:
        return 0
    return int(conn.execute("SELECT COUNT(*) FROM paper_trades WHERE UPPER(COALESCE(status, '')) <> 'OPEN'").fetchone()[0])


def _write_live_config(path: Path, *, strategy: str, snapshot_db: Path, results_db: Path, volatility_hedge_config: VolatilityHedgeConfig | None = None) -> None:
    lines = [
        'mode = "live_paper"',
        f'strategy = "{strategy}"',
        'run_id = "live"',
        f'snapshot_db = "{snapshot_db}"',
        f'results_db = "{results_db}"',
        "",
    ]
    if volatility_hedge_config is not None:
        lines.append("[volatility_hedge]")
        for field in fields(VolatilityHedgeConfig):
            value = getattr(volatility_hedge_config, field.name)
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, str):
                rendered = json.dumps(value)
            elif value is None:
                lines.append(f"# {field.name} = unset")
                continue
            else:
                rendered = str(value)
            lines.append(f"{field.name} = {rendered}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
