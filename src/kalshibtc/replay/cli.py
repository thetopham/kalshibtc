from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..backtest.metrics import compute_metrics
from ..config import BotConfig, RiskLimits
from ..datafeed.models import OrderBookSnapshot, Tick
from ..execution.paper import PaperFill
from ..execution.risk import CapitalConstraints, CapitalState, RiskDecision, RiskManager
from ..market.contract import ContractWindow
from ..runtime_paths import DEFAULT_FEED_DB, DEFAULT_RUNS_DIR, resolve_feed_db, resolve_runs_dir
from ..strategy.registry import create_strategy, strategy_names
from ..strategy.signals import Signal
from .replay import ReplayEngine
from .settlement import compute_portfolio_settlement, estimate_replay_fill_pnls


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kbtc-replay",
        description="Replay a recorded Kalshi BTC 1s feed DB through a strategy into an immutable run directory.",
    )
    parser.add_argument("--feed-db", default=None, help=f"Feed SQLite DB. Default: {DEFAULT_FEED_DB}")
    parser.add_argument("--runs-dir", default=None, help=f"Runs root directory. Default: {DEFAULT_RUNS_DIR}")
    parser.add_argument("--strategy", default="simple_directional", choices=strategy_names())
    parser.add_argument("--run-id", default=None, help="Run directory name. Defaults to current UTC timestamp.")
    parser.add_argument("--from", dest="from_ts", default=None, help="Inclusive ISO timestamp lower bound.")
    parser.add_argument("--to", dest="to_ts", default=None, help="Inclusive ISO timestamp upper bound.")
    parser.add_argument("--base-size-dollars", type=float, default=25.0)
    parser.add_argument("--max-position-dollars", type=float, default=25.0)
    parser.add_argument("--max-open-positions", type=int, default=1)
    parser.add_argument("--max-spread", type=float, default=0.05)
    parser.add_argument("--starting-bankroll", type=float, default=None, help="Starting bankroll for capital-aware replay, e.g. 200.")
    parser.add_argument("--max-capital-at-risk", type=float, default=None, help="Maximum locked capital allowed at once.")
    parser.add_argument("--fee-per-contract", type=float, default=0.0, help="Flat fee per contract filled.")
    parser.add_argument("--fee-rate", type=float, default=0.0, help="Fee as decimal fraction of notional filled.")
    parser.add_argument("--per-market-max-exposure", type=float, default=None, help="Maximum locked capital per market ticker.")
    parser.add_argument("--daily-loss-cap", type=float, default=None, help="Stop new trades after this daily realized loss cap is reached.")
    parser.add_argument("--strategy-param", action="append", default=[], help="Strategy-specific key=value override. Currently used for simple_inventory_mm knobs.")
    parser.add_argument(
        "--no-settle-on-market-rollover",
        action="store_true",
        help="Do not reset replay open-position count when the market ticker changes.",
    )
    parser.add_argument(
        "--settlements-from-feed-db",
        action="store_true",
        help="Prefer official Kalshi settlements from feed DB market_settlements for PnL metrics.",
    )
    parser.add_argument(
        "--fill-timing",
        choices=["same-tick", "next-tick"],
        default="same-tick",
        help="Replay fill timing. next-tick executes allowed signals on the following snapshot to avoid same-tick hindsight.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON summary.")
    parser.add_argument(
        "--update-research-journal",
        action="store_true",
        help="After writing this run, regenerate Git-friendly research/run_index.csv and reports/latest.md.",
    )
    parser.add_argument("--research-dir", default="research", help="Research journal output directory. Default: research")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing an existing run directory.")
    args = parser.parse_args(argv)

    feed_db = resolve_feed_db(args.feed_db)
    runs_dir = resolve_runs_dir(args.runs_dir)
    run_id = args.run_id or datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = runs_dir / args.strategy / run_id
    if run_dir.exists() and not args.overwrite:
        print(f"run directory already exists: {run_dir}", file=sys.stderr)
        return 2
    run_dir.mkdir(parents=True, exist_ok=True)

    rows = _load_snapshot_rows(feed_db, from_ts=args.from_ts, to_ts=args.to_ts)
    ticks, books, contract = _rows_to_replay_inputs(rows)
    strategy_params = _parse_strategy_params(args.strategy_param)
    strategy = create_strategy(args.strategy, params=strategy_params)
    risk = RiskManager(
        RiskLimits(
            base_size_dollars=args.base_size_dollars,
            max_position_dollars=args.max_position_dollars,
            max_open_positions=args.max_open_positions,
            max_spread=args.max_spread,
        )
    )
    capital_state = _capital_state_from_args(args)
    report = ReplayEngine(
        config=BotConfig(),
        contract=contract,
        strategies=[strategy],
        risk_manager=risk,
        settle_on_market_rollover=not args.no_settle_on_market_rollover,
        fill_timing=args.fill_timing,
        capital_state=capital_state,
    ).run(ticks=ticks, books=books)

    results_db = run_dir / "results.sqlite3"
    _write_results(results_db, report.results, strategies=[strategy])
    config_text = _config_text(args=args, feed_db=feed_db, run_id=run_id)
    (run_dir / "config.toml").write_text(config_text, encoding="utf-8")
    settlement_rows = _load_official_settlement_rows(feed_db) if args.settlements_from_feed_db else rows
    portfolio_settlement = compute_portfolio_settlement(report.fills, settlement_rows)
    if capital_state is not None:
        capital_state.realized_pnl = float(portfolio_settlement.get("aggregate", {}).get("realized_pnl", 0.0) or 0.0)
    _write_portfolio_settlement(results_db, portfolio_settlement)
    (run_dir / "portfolio_settlement.json").write_text(
        json.dumps(portfolio_settlement["aggregate"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    metrics = _metrics_payload(
        report=report,
        feed_db=feed_db,
        run_dir=run_dir,
        strategy=args.strategy,
        max_open_positions=args.max_open_positions,
        settlement_rows=settlement_rows,
        strategies=[strategy],
        portfolio_settlement=portfolio_settlement,
        capital_state=capital_state,
    )
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.update_research_journal:
        from ..research.journal import build_research_journal

        build_research_journal(runs_dir=runs_dir, out_dir=Path(args.research_dir))

    if args.json:
        print(json.dumps(metrics, sort_keys=True))
    else:
        print(
            "kbtc_replay "
            f"strategy={args.strategy} snapshots={metrics['snapshots']} "
            f"signals={metrics['signals']} fills={metrics['fills']} run_dir={run_dir}"
        )
    return 0


def _load_snapshot_rows(feed_db: Path, *, from_ts: str | None, to_ts: str | None) -> list[sqlite3.Row]:
    if not feed_db.exists():
        raise FileNotFoundError(f"feed DB not found: {feed_db}")
    where: list[str] = []
    params: list[str] = []
    if from_ts:
        where.append("ts >= ?")
        params.append(from_ts)
    if to_ts:
        where.append("ts <= ?")
        params.append(to_ts)
    sql = "SELECT * FROM realtime_snapshots_1s"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts ASC, market_ticker ASC"
    with sqlite3.connect(f"file:{feed_db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return list(conn.execute(sql, params))


def _load_official_settlement_rows(feed_db: Path) -> list[sqlite3.Row]:
    if not feed_db.exists():
        raise FileNotFoundError(f"feed DB not found: {feed_db}")
    with sqlite3.connect(f"file:{feed_db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_settlements'"
        ).fetchone()
        if exists is None:
            return []
        return list(
            conn.execute(
                """
                SELECT *
                FROM market_settlements
                WHERE source = 'kalshi_api'
                  AND status = 'settled_official'
                  AND winning_side IN ('yes', 'no')
                """
            )
        )


def _rows_to_replay_inputs(rows: list[sqlite3.Row]) -> tuple[list[Tick], list[OrderBookSnapshot], ContractWindow]:
    if not rows:
        raise ValueError("feed DB query returned no snapshots")
    first = rows[0]
    contract = ContractWindow(
        ticker=str(first["market_ticker"]),
        strike=float(first["strike"]),
        close_time=_parse_dt(first["market_close_time"]),
        open_time=_parse_dt(first["market_open_time"]) if _has_column(first, "market_open_time") and first["market_open_time"] else None,
    )
    ticks: list[Tick] = []
    books: list[OrderBookSnapshot] = []
    for row in rows:
        ts = _parse_dt(row["ts"])
        ticks.append(
            Tick(
                ts=ts,
                price=float(row["btc_price"]),
                source="feed_replay",
                symbol="BTC-USD",
                raw=_row_market_metadata(row, base_raw=_json_or_empty(row["raw_json"] if _has_column(row, "raw_json") else None)),
            )
        )
        books.append(
            OrderBookSnapshot(
                ts=ts,
                market_ticker=str(row["market_ticker"]),
                yes_bid=_optional_float(row, "yes_bid"),
                yes_ask=_optional_float(row, "yes_ask"),
                no_bid=_optional_float(row, "no_bid"),
                no_ask=_optional_float(row, "no_ask"),
                sequence=_optional_int(row, "orderbook_sequence"),
                raw=_row_market_metadata(row, base_raw=_json_or_empty(row["raw_json"] if _has_column(row, "raw_json") else None)),
            )
        )
    return ticks, books, contract


def _write_results(path: Path, results: list[Any], *, strategies: Sequence[Any] | None = None) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE replay_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                strategy TEXT NOT NULL,
                side TEXT NOT NULL,
                confidence REAL NOT NULL,
                reason TEXT NOT NULL,
                allowed INTEGER NOT NULL,
                blocked_by_json TEXT NOT NULL,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE replay_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                strategy TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                contracts REAL NOT NULL,
                notional REAL NOT NULL,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE volatility_features (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                atr_1m REAL NOT NULL,
                atr_expansion_rate REAL NOT NULL,
                distance_from_strike REAL NOT NULL,
                distance_from_strike_abs REAL NOT NULL,
                velocity_away_from_strike REAL NOT NULL,
                velocity_slowdown REAL NOT NULL,
                macd_histogram REAL NOT NULL,
                macd_slope REAL NOT NULL,
                time_to_expiry_seconds REAL NOT NULL,
                volatility_regime_score REAL NOT NULL,
                expansion_regime INTEGER NOT NULL,
                stabilization_regime INTEGER NOT NULL,
                compression_regime INTEGER NOT NULL
            );
            CREATE TABLE inventory_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                reason TEXT NOT NULL,
                target_notional REAL NOT NULL,
                estimated_shares REAL NOT NULL,
                entry_price REAL,
                raw_json TEXT NOT NULL
            );
            CREATE TABLE inventory_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                above_qty REAL NOT NULL,
                above_avg_price REAL NOT NULL,
                below_qty REAL NOT NULL,
                below_avg_price REAL NOT NULL,
                blended_basis REAL,
                imbalance_ratio REAL NOT NULL
            );
            CREATE TABLE inventory_equity_curve (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                market_ticker TEXT NOT NULL,
                mtm_value REAL NOT NULL,
                cost_basis REAL NOT NULL,
                unrealized_pnl REAL NOT NULL,
                above_mark REAL,
                below_mark REAL
            );
            CREATE TABLE portfolio_settlement_by_market (
                market_ticker TEXT PRIMARY KEY,
                settlement_result TEXT,
                settlement_source TEXT NOT NULL,
                yes_contracts REAL NOT NULL,
                no_contracts REAL NOT NULL,
                avg_yes_entry REAL,
                avg_no_entry REAL,
                yes_cost REAL NOT NULL,
                no_cost REAL NOT NULL,
                total_cost REAL NOT NULL,
                gross_payout REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                paired_contracts REAL NOT NULL,
                paired_cost REAL,
                paired_locked_edge REAL NOT NULL,
                raw_net_contracts REAL NOT NULL,
                final_unpaired_yes_contracts REAL NOT NULL,
                final_unpaired_no_contracts REAL NOT NULL
            );
            """
        )
        for result in results:
            signal: Signal = result.signal
            risk: RiskDecision = result.risk
            fill: PaperFill | None = result.fill
            ts = fill.ts.isoformat() if fill is not None else ""
            raw = {
                "signal": signal.__dict__,
                "risk": {
                    "allowed": risk.allowed,
                    "size_dollars": risk.size_dollars,
                    "entry_price": risk.entry_price,
                    "blocked_by": risk.blocked_by,
                },
            }
            conn.execute(
                """
                INSERT INTO replay_signals (
                    ts, strategy, side, confidence, reason, allowed, blocked_by_json, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    signal.strategy,
                    signal.side,
                    signal.confidence,
                    signal.reason,
                    1 if risk.allowed else 0,
                    json.dumps(risk.blocked_by, sort_keys=True),
                    json.dumps(raw, sort_keys=True),
                ),
            )
            if fill is not None:
                conn.execute(
                    """
                    INSERT INTO replay_fills (
                        ts, strategy, side, entry_price, contracts, notional, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fill.ts.isoformat(),
                        signal.strategy,
                        fill.side,
                        fill.entry_price,
                        fill.contracts,
                        fill.notional,
                        json.dumps(fill.__dict__, default=str, sort_keys=True),
                    ),
                )
        _write_inventory_research_tables(conn, strategies or [])


def _write_portfolio_settlement(path: Path, settlement: Mapping[str, Any]) -> None:
    fields = [
        "market_ticker",
        "settlement_result",
        "settlement_source",
        "yes_contracts",
        "no_contracts",
        "avg_yes_entry",
        "avg_no_entry",
        "yes_cost",
        "no_cost",
        "total_cost",
        "gross_payout",
        "realized_pnl",
        "paired_contracts",
        "paired_cost",
        "paired_locked_edge",
        "raw_net_contracts",
        "final_unpaired_yes_contracts",
        "final_unpaired_no_contracts",
    ]
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE contract_metrics (
                market_ticker TEXT PRIMARY KEY,
                date TEXT NOT NULL,
                settlement_result TEXT,
                settlement_source TEXT NOT NULL,
                fills INTEGER NOT NULL,
                total_cost REAL NOT NULL,
                gross_payout REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                paired_locked_edge REAL NOT NULL,
                final_unpaired_yes_contracts REAL NOT NULL,
                final_unpaired_no_contracts REAL NOT NULL,
                max_abs_raw_net_contracts REAL NOT NULL
            );
            CREATE TABLE daily_metrics (
                date TEXT PRIMARY KEY,
                contracts INTEGER NOT NULL,
                settled_contracts INTEGER NOT NULL,
                total_cost REAL NOT NULL,
                gross_payout REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                paired_locked_edge REAL NOT NULL,
                final_unpaired_yes_contracts REAL NOT NULL,
                final_unpaired_no_contracts REAL NOT NULL,
                max_abs_raw_net_contracts REAL NOT NULL,
                settlement_sources_json TEXT NOT NULL
            );
            """
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO portfolio_settlement_by_market (
                market_ticker, settlement_result, settlement_source, yes_contracts, no_contracts,
                avg_yes_entry, avg_no_entry, yes_cost, no_cost, total_cost, gross_payout,
                realized_pnl, paired_contracts, paired_cost, paired_locked_edge, raw_net_contracts,
                final_unpaired_yes_contracts, final_unpaired_no_contracts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [tuple(row.get(field) for field in fields) for row in settlement.get("markets", [])],
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO contract_metrics (
                market_ticker, date, settlement_result, settlement_source, fills, total_cost,
                gross_payout, realized_pnl, paired_locked_edge, final_unpaired_yes_contracts,
                final_unpaired_no_contracts, max_abs_raw_net_contracts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row.get("market_ticker"),
                    row.get("date") or "unknown",
                    row.get("settlement_result"),
                    row.get("settlement_source"),
                    int(float(row.get("yes_contracts") or 0.0) > 0) + int(float(row.get("no_contracts") or 0.0) > 0),
                    row.get("total_cost"),
                    row.get("gross_payout"),
                    row.get("realized_pnl"),
                    row.get("paired_locked_edge"),
                    row.get("final_unpaired_yes_contracts"),
                    row.get("final_unpaired_no_contracts"),
                    abs(float(row.get("raw_net_contracts") or 0.0)),
                )
                for row in settlement.get("markets", [])
            ],
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO daily_metrics (
                date, contracts, settled_contracts, total_cost, gross_payout, realized_pnl,
                paired_locked_edge, final_unpaired_yes_contracts, final_unpaired_no_contracts,
                max_abs_raw_net_contracts, settlement_sources_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row.get("date"),
                    row.get("contracts"),
                    row.get("settled_contracts"),
                    row.get("total_cost"),
                    row.get("gross_payout"),
                    row.get("realized_pnl"),
                    row.get("paired_locked_edge"),
                    row.get("final_unpaired_yes_contracts"),
                    row.get("final_unpaired_no_contracts"),
                    row.get("max_abs_raw_net_contracts"),
                    json.dumps(row.get("settlement_sources", []), sort_keys=True),
                )
                for row in settlement.get("daily", [])
            ],
        )


def _write_inventory_research_tables(conn: sqlite3.Connection, strategies: Sequence[Any]) -> None:
    for strategy in strategies:
        for decision in getattr(strategy, "decisions", []):
            features = decision.features.as_dict()
            conn.execute(
                """
                INSERT INTO volatility_features (
                    ts, market_ticker, atr_1m, atr_expansion_rate, distance_from_strike,
                    distance_from_strike_abs, velocity_away_from_strike, velocity_slowdown,
                    macd_histogram, macd_slope, time_to_expiry_seconds,
                    volatility_regime_score, expansion_regime, stabilization_regime, compression_regime
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.ts,
                    decision.market_ticker,
                    features["atr_1m"],
                    features["atr_expansion_rate"],
                    features["distance_from_strike"],
                    features["distance_from_strike_abs"],
                    features["velocity_away_from_strike"],
                    features["velocity_slowdown"],
                    features["macd_histogram"],
                    features["macd_slope"],
                    features["time_to_expiry_seconds"],
                    features["volatility_regime_score"],
                    1 if features["expansion_regime"] else 0,
                    1 if features["stabilization_regime"] else 0,
                    1 if features["compression_regime"] else 0,
                ),
            )
            conn.execute(
                """
                INSERT INTO inventory_decisions (
                    ts, market_ticker, side, reason, target_notional, estimated_shares,
                    entry_price, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.ts,
                    decision.market_ticker,
                    decision.side,
                    decision.reason,
                    decision.target_notional,
                    decision.estimated_shares,
                    decision.entry_price,
                    json.dumps({"features": features}, sort_keys=True),
                ),
            )
        for position in getattr(strategy, "position_history", []):
            above_qty = getattr(position, "above_qty", getattr(position, "yes_qty", 0.0))
            above_avg_price = getattr(position, "above_avg_price", getattr(position, "yes_avg_price", 0.0)) or 0.0
            below_qty = getattr(position, "below_qty", getattr(position, "no_qty", 0.0))
            below_avg_price = getattr(position, "below_avg_price", getattr(position, "no_avg_price", 0.0)) or 0.0
            conn.execute(
                """
                INSERT INTO inventory_positions (
                    ts, market_ticker, above_qty, above_avg_price, below_qty,
                    below_avg_price, blended_basis, imbalance_ratio
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position.ts,
                    position.market_ticker,
                    above_qty,
                    above_avg_price,
                    below_qty,
                    below_avg_price,
                    _position_blended_basis(position),
                    _position_imbalance(position),
                ),
            )
        for equity in getattr(strategy, "equity_curve", []):
            conn.execute(
                """
                INSERT INTO inventory_equity_curve (
                    ts, market_ticker, mtm_value, cost_basis, unrealized_pnl, above_mark, below_mark
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    equity.ts,
                    equity.market_ticker,
                    equity.mtm_value,
                    equity.cost_basis,
                    equity.unrealized_pnl,
                    equity.above_mark,
                    equity.below_mark,
                ),
            )


def _metrics_payload(
    *,
    report: Any,
    feed_db: Path,
    run_dir: Path,
    strategy: str,
    max_open_positions: int,
    settlement_rows: Sequence[Mapping[str, Any] | Any] | None = None,
    strategies: Sequence[Any] | None = None,
    portfolio_settlement: Mapping[str, Any] | None = None,
    capital_state: CapitalState | None = None,
) -> dict[str, Any]:
    settled_fills = estimate_replay_fill_pnls(report.fills, settlement_rows or [])
    kalshi_settled_fills = [fill for fill in settled_fills if fill.get("settlement_source") == "kalshi_api"]
    metric_fills = kalshi_settled_fills or settled_fills
    institutional_metrics: dict[str, Any] = dict(compute_metrics(metric_fills))
    settlement_sources = sorted(
        {
            str(fill.get("settlement_source"))
            for fill in settled_fills
            if fill.get("settlement_source")
        }
    )
    if settlement_sources:
        institutional_metrics["settlement_sources"] = settlement_sources
        institutional_metrics["settlement_source"] = (
            settlement_sources[0] if len(settlement_sources) == 1 else "mixed"
        )
    institutional_metrics["settled_trades"] = sum(
        1
        for fill in settled_fills
        if fill.get("settlement_source") in {"replay_final_snapshot", "kalshi_api"}
    )
    research_metrics = _inventory_research_metrics(strategies or [])
    payload = {
        "feed_db": str(feed_db),
        "run_dir": str(run_dir),
        "run_id": run_dir.name,
        "strategy": strategy,
        "snapshots": report.total_ticks,
        "signals": report.total_signals,
        "fills": len(report.fills),
        "settled_positions": getattr(report, "settled_positions", 0),
        "notional": round(sum(fill.notional for fill in report.fills), 6),
        "max_open_positions": max_open_positions,
        "institutional_metrics": institutional_metrics,
    }
    if research_metrics:
        payload["research_metrics"] = research_metrics
    if portfolio_settlement:
        payload["portfolio_settlement"] = portfolio_settlement.get("aggregate", {})
        contract_rows = list(portfolio_settlement.get("markets", []))
        daily_rows = list(portfolio_settlement.get("daily", []))
        payload["contract_metrics"] = {"contracts": len(contract_rows), "rows": contract_rows}
        payload["daily_metrics"] = {"days": len(daily_rows), "rows": daily_rows}
    if capital_state is not None:
        payload["starting_bankroll"] = capital_state.constraints.starting_bankroll
        payload["capital_metrics"] = capital_state.metrics()
    return payload


def _capital_state_from_args(args: argparse.Namespace) -> CapitalState | None:
    constraints = CapitalConstraints(
        starting_bankroll=args.starting_bankroll,
        max_capital_at_risk=args.max_capital_at_risk,
        fee_per_contract=max(0.0, float(args.fee_per_contract or 0.0)),
        fee_rate=max(0.0, float(args.fee_rate or 0.0)),
        per_market_max_exposure=args.per_market_max_exposure,
        daily_loss_cap=args.daily_loss_cap,
    )
    return CapitalState(constraints) if constraints.enabled else None


def _inventory_research_metrics(strategies: Sequence[Any]) -> dict[str, Any]:
    feature_rows = 0
    decisions = 0
    position_rows = 0
    equity_rows = 0
    max_imbalance = 0.0
    latest_blended_basis = None
    for strategy in strategies:
        feature_rows += len(getattr(strategy, "feature_history", []))
        decisions_list = getattr(strategy, "decisions", None)
        if decisions_list is None:
            decisions_list = getattr(strategy, "decision_history", [])
        decisions += sum(1 for decision in decisions_list if _decision_target_notional(decision) > 0)
        positions = getattr(strategy, "position_history", [])
        position_rows += len(positions)
        equity_rows += len(getattr(strategy, "equity_curve", []))
        if positions:
            max_imbalance = max(max_imbalance, max(_position_imbalance(position) for position in positions))
            latest_blended_basis = _position_blended_basis(positions[-1])
    if feature_rows == decisions == position_rows == equity_rows == 0:
        return {}
    return {
        "volatility_feature_rows": feature_rows,
        "inventory_decisions": decisions,
        "inventory_position_rows": position_rows,
        "inventory_equity_rows": equity_rows,
        "max_inventory_imbalance_ratio": round(max_imbalance, 6),
        "latest_blended_basis": latest_blended_basis,
        "tracked_metrics": [
            "inventory additions vs ATR spikes",
            "inventory additions vs velocity-away-from-strike",
            "inventory additions vs MACD histogram extremes",
            "mark-to-market equity curve",
            "realized/unrealized PnL",
            "blended basis over time",
            "inventory imbalance ratio",
            "recovery after volatility shocks",
            "compression after directional expansion",
        ],
    }


def _decision_target_notional(decision: Any) -> float:
    if isinstance(decision, Mapping):
        value = decision.get("target_notional")
    else:
        value = getattr(decision, "target_notional", 0.0)
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _position_imbalance(position: Any) -> float:
    value = getattr(position, "imbalance_ratio", None)
    if value is not None:
        return float(value)
    yes_qty = float(getattr(position, "yes_qty", getattr(position, "above_qty", 0.0)) or 0.0)
    no_qty = float(getattr(position, "no_qty", getattr(position, "below_qty", 0.0)) or 0.0)
    smaller = min(yes_qty, no_qty)
    larger = max(yes_qty, no_qty)
    return larger / smaller if smaller > 0 else (larger if larger > 0 else 0.0)


def _position_blended_basis(position: Any) -> float | None:
    value = getattr(position, "blended_basis", None)
    if value is not None:
        return value
    return getattr(position, "combined_basis", None)


def _config_text(*, args: argparse.Namespace, feed_db: Path, run_id: str) -> str:
    return "\n".join(
        [
            f'feed_db = "{feed_db}"',
            f'strategy = "{args.strategy}"',
            f'run_id = "{run_id}"',
            f'from_ts = "{args.from_ts or ""}"',
            f'to_ts = "{args.to_ts or ""}"',
            f"base_size_dollars = {args.base_size_dollars}",
            f"max_position_dollars = {args.max_position_dollars}",
            f"max_open_positions = {args.max_open_positions}",
            f"max_spread = {args.max_spread}",
            f"starting_bankroll = {_toml_float_or_empty(args.starting_bankroll)}",
            f"max_capital_at_risk = {_toml_float_or_empty(args.max_capital_at_risk)}",
            f"fee_per_contract = {float(args.fee_per_contract or 0.0)}",
            f"fee_rate = {float(args.fee_rate or 0.0)}",
            f"per_market_max_exposure = {_toml_float_or_empty(args.per_market_max_exposure)}",
            f"daily_loss_cap = {_toml_float_or_empty(args.daily_loss_cap)}",
            f"strategy_params = {json.dumps(_parse_strategy_params(args.strategy_param), sort_keys=True)}",
            f"settle_on_market_rollover = {str(not args.no_settle_on_market_rollover).lower()}",
            f"settlements_from_feed_db = {str(args.settlements_from_feed_db).lower()}",
            f"fill_timing = \"{args.fill_timing}\"",
            "",
        ]
    )


def _toml_float_or_empty(value: float | None) -> str:
    return '""' if value is None else str(float(value))


def _parse_strategy_params(items: Sequence[str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"strategy param must be key=value: {item}")
        key, raw_value = item.split("=", 1)
        key = key.strip().replace("-", "_")
        raw_value = raw_value.strip()
        if not key:
            raise ValueError(f"strategy param key is empty: {item}")
        try:
            params[key] = json.loads(raw_value)
        except json.JSONDecodeError:
            try:
                params[key] = float(raw_value)
            except ValueError:
                params[key] = raw_value
    return params


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _has_column(row: sqlite3.Row, name: str) -> bool:
    return name in row.keys()


def _row_market_metadata(row: sqlite3.Row, *, base_raw: dict[str, Any]) -> dict[str, Any]:
    raw = dict(base_raw)
    for key in (
        "market_ticker",
        "market_open_time",
        "market_close_time",
        "strike",
        "target_price",
        "distance_from_strike",
        "seconds_to_close",
        "slope_30s",
        "btc_velocity_30s",
    ):
        if _has_column(row, key) and row[key] is not None:
            raw[key] = row[key]
    return raw


def _optional_float(row: sqlite3.Row, name: str) -> float | None:
    if not _has_column(row, name) or row[name] is None:
        return None
    return float(row[name])


def _optional_int(row: sqlite3.Row, name: str) -> int | None:
    if not _has_column(row, name) or row[name] is None:
        return None
    return int(row[name])


def _json_or_empty(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


if __name__ == "__main__":
    raise SystemExit(main())
