from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter, deque
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ..datafeed.models import OrderBookSnapshot, Tick
from ..execution.hedge_paper import HedgePaperExecutor
from ..market.contract import ContractWindow
from ..market.state import MarketState
from ..portfolio.hedge_position import HedgePosition
from ..runtime_paths import DEFAULT_FEED_DB, DEFAULT_RUNS_DIR, resolve_feed_db, resolve_runs_dir
from ..strategy.hedge_volatility_v0 import HedgeVolatilityConfig, HedgeVolatilityV0

STRATEGY_NAME = "hedge_volatility_v0"
SettlementSource = Literal["manual", "kalshi", "coinbase_1m_avg", "chainlink_1m_avg"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kbtc-hedge-v0-replay",
        description="Replay hedge_volatility_v0 against the recorded 1s feed into an isolated results DB.",
    )
    parser.add_argument("--feed-db", default=None, help=f"Feed SQLite DB. Default: {DEFAULT_FEED_DB}")
    parser.add_argument("--runs-dir", default=None, help=f"Runs root directory. Default: {DEFAULT_RUNS_DIR}")
    parser.add_argument("--run-id", default=None, help="Run directory name. Defaults to current UTC timestamp.")
    parser.add_argument("--from", dest="from_ts", default=None, help="Inclusive ISO timestamp lower bound.")
    parser.add_argument("--to", dest="to_ts", default=None, help="Inclusive ISO timestamp upper bound.")
    parser.add_argument("--market-ticker", default=None, help="Optional market_ticker filter.")
    parser.add_argument("--min-strike", type=float, default=1000.0)
    parser.add_argument("--max-strike", type=float, default=1_000_000.0)
    parser.add_argument("--max-projected-pair-cost", type=float, default=0.98)
    parser.add_argument("--seed-max-pair-cost", type=float, default=1.10)
    parser.add_argument("--trend-contracts", type=float, default=3.0)
    parser.add_argument("--countertrend-contracts", type=float, default=2.0)
    parser.add_argument("--add-contracts", type=float, default=1.0)
    parser.add_argument("--max-contracts-per-market", type=float, default=25.0)
    parser.add_argument("--max-imbalance-ratio", type=float, default=1.5)
    parser.add_argument("--max-unpaired-contracts", type=float, default=2.0)
    parser.add_argument("--max-leg-ask", type=float, default=0.95)
    parser.add_argument("--cheaper-side-only-adds", action="store_true")
    parser.add_argument("--min-abs-slope", type=float, default=0.0)
    parser.add_argument("--min-recent-volatility", type=float, default=0.0)
    parser.add_argument("--min-distance-from-strike", type=float, default=0.0)
    parser.add_argument("--min-seconds-to-expiry", type=float, default=45.0)
    parser.add_argument("--max-seconds-to-expiry", type=float, default=14.5 * 60.0)
    parser.add_argument("--rebalance-seconds-to-expiry", type=float, default=180.0)
    parser.add_argument("--max-balance-add-pair-cost", type=float, default=1.02)
    parser.add_argument("--target-final-unpaired-contracts", type=float, default=0.0)
    parser.add_argument(
        "--no-force-balance-near-expiry",
        action="store_true",
        help="Disable expiry-window smaller-side force balancing.",
    )
    parser.add_argument(
        "--no-allow-balance-add-above-target",
        action="store_true",
        help="Require expiry balance adds to stay below target pair cost.",
    )
    parser.add_argument("--volatility-window", type=int, default=30)
    parser.add_argument("--settlement-price", type=float, default=None)
    parser.add_argument("--settlements-csv", default=None, help="CSV with market_ticker,settlement_price columns.")
    parser.add_argument(
        "--settlements-from-feed-db",
        action="store_true",
        help="Load per-market settlements from feed DB market_settlements.",
    )
    parser.add_argument(
        "--settlement-source",
        choices=["manual", "kalshi", "coinbase_1m_avg", "chainlink_1m_avg"],
        default="manual",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON summary.")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing an existing run directory.")
    args = parser.parse_args(argv)
    settlement_arg_count = sum(
        1
        for enabled in (
            args.settlement_price is not None,
            bool(args.settlements_csv),
            bool(args.settlements_from_feed_db),
        )
        if enabled
    )
    if settlement_arg_count > 1:
        print("settlement args are mutually exclusive and cannot be used together", file=sys.stderr)
        return 2

    feed_db = resolve_feed_db(args.feed_db)
    runs_dir = resolve_runs_dir(args.runs_dir)
    run_id = args.run_id or datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    config = HedgeVolatilityConfig(
        max_projected_pair_cost=args.max_projected_pair_cost,
        target_pair_cost=args.max_projected_pair_cost,
        seed_max_pair_cost=args.seed_max_pair_cost,
        trend_contracts=args.trend_contracts,
        countertrend_contracts=args.countertrend_contracts,
        add_contracts=args.add_contracts,
        max_contracts_per_market=args.max_contracts_per_market,
        max_imbalance_ratio=args.max_imbalance_ratio,
        max_unpaired_contracts=args.max_unpaired_contracts,
        max_leg_ask=args.max_leg_ask,
        cheaper_side_only_adds=args.cheaper_side_only_adds,
        min_abs_slope=args.min_abs_slope,
        min_recent_volatility=args.min_recent_volatility,
        min_distance_from_strike=args.min_distance_from_strike,
        min_seconds_to_expiry=args.min_seconds_to_expiry,
        max_seconds_to_expiry=args.max_seconds_to_expiry,
        rebalance_seconds_to_expiry=args.rebalance_seconds_to_expiry,
        force_balance_near_expiry=not args.no_force_balance_near_expiry,
        allow_balance_add_above_target=not args.no_allow_balance_add_above_target,
        max_balance_add_pair_cost=args.max_balance_add_pair_cost,
        target_final_unpaired_contracts=args.target_final_unpaired_contracts,
    )
    try:
        summary = run_replay(
            feed_db=feed_db,
            runs_dir=runs_dir,
            run_id=run_id,
            from_ts=args.from_ts,
            to_ts=args.to_ts,
            market_ticker=args.market_ticker,
            min_strike=args.min_strike,
            max_strike=args.max_strike,
            config=config,
            volatility_window=args.volatility_window,
            settlement_price=args.settlement_price,
            settlements_csv=args.settlements_csv,
            settlements_from_feed_db=args.settlements_from_feed_db,
            settlement_source=args.settlement_source,
            overwrite=args.overwrite,
        )
    except FileExistsError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(summary, sort_keys=True))
    else:
        print(_format_summary(summary))
    return 0


def run_replay(
    *,
    feed_db: str | Path,
    runs_dir: str | Path,
    run_id: str,
    from_ts: str | None,
    to_ts: str | None,
    market_ticker: str | None = None,
    min_strike: float = 1000.0,
    max_strike: float = 1_000_000.0,
    config: HedgeVolatilityConfig | None = None,
    volatility_window: int = 30,
    settlement_price: float | None = None,
    settlements_csv: str | Path | None = None,
    settlements_from_feed_db: bool = False,
    settlement_source: SettlementSource = "manual",
    overwrite: bool = False,
) -> dict[str, Any]:
    settlement_arg_count = sum(1 for enabled in (settlement_price is not None, settlements_csv is not None, settlements_from_feed_db) if enabled)
    if settlement_arg_count > 1:
        raise ValueError("settlement args are mutually exclusive")
    feed_path = Path(feed_db)
    run_dir = Path(runs_dir) / "replay" / STRATEGY_NAME / run_id
    if run_dir.exists() and not overwrite:
        raise FileExistsError(f"run directory already exists: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    results_db = run_dir / "results.sqlite3"
    if results_db.exists() and overwrite:
        results_db.unlink()

    strategy = HedgeVolatilityV0(config or HedgeVolatilityConfig())
    positions: dict[str, HedgePosition] = {}
    strikes: dict[str, float] = {}
    reject_counts: Counter[str] = Counter()
    snapshots_processed = 0
    skipped_bad_strike_rows = _count_bad_strike_rows(
        feed_path,
        from_ts=from_ts,
        to_ts=to_ts,
        market_ticker=market_ticker,
        min_strike=min_strike,
        max_strike=max_strike,
    )
    fills = 0
    seed_fills = 0
    add_fills = 0
    expiry_balance_fills = 0
    paired_costs: list[float] = []
    unpaired_counts_seen: list[float] = []
    buy_both_costs: list[float] = []
    orderbook_snapshots_logged = 0
    volatility_tracker = RecentVolatility(window=volatility_window)

    diagnostics_tracker = ReplayDiagnostics(window=30)
    with HedgePaperExecutor(results_db=results_db, strategy_name=strategy.name) as executor:
        _initialize_orderbook_snapshot_schema(executor.conn)
        for row in _load_snapshot_rows(
            feed_path,
            from_ts=from_ts,
            to_ts=to_ts,
            market_ticker=market_ticker,
            min_strike=min_strike,
            max_strike=max_strike,
        ):
            state = _state_from_row(row)
            strikes[state.orderbook.market_ticker] = state.strike
            snapshots_processed += 1
            recent_volatility = volatility_tracker.add(state.orderbook.market_ticker, state.price)
            diagnostics = diagnostics_tracker.add(state)
            buy_both_cost = _buy_both_cost(state)
            if buy_both_cost is not None:
                buy_both_costs.append(buy_both_cost)
            executor.conn.execute(
                """
                INSERT INTO hedge_orderbook_snapshots (
                    ts, market_ticker, btc_price, strike, seconds_to_close, slope_30s,
                    recent_volatility, yes_bid, yes_ask, no_bid, no_ask,
                    buy_both_cost, sell_both_credit, distance_from_strike,
                    time_to_expiry, abs_slope_30s, abs_distance_from_strike,
                    distance_velocity_30s, abs_distance_velocity_30s,
                    atr_30s, atr_expansion_30s, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _orderbook_snapshot_params(state, recent_volatility=recent_volatility, diagnostics=diagnostics),
            )
            orderbook_snapshots_logged += 1
            position = positions.setdefault(
                state.orderbook.market_ticker,
                HedgePosition(market_ticker=state.orderbook.market_ticker),
            )
            before_fill_count = len(position.fills)
            decisions = strategy.decide(state, position, recent_volatility=recent_volatility)
            if decisions:
                for decision in decisions:
                    executor.apply(state=state, position=position, decision=decision)
                    fills += 1
                    if decision.reason.startswith("seed_"):
                        seed_fills += 1
                    elif decision.reason == "expiry_balance_add_smaller_side":
                        expiry_balance_fills += 1
                        add_fills += 1
                    else:
                        add_fills += 1
                    if position.combined_average_cost is not None:
                        paired_costs.append(position.combined_average_cost)
                if position.fills:
                    unpaired_counts_seen.append(abs(position.yes_contracts - position.no_contracts))
            else:
                reason = _infer_reject_reason(
                    state,
                    position=position,
                    config=strategy.config,
                    recent_volatility=recent_volatility,
                )
                reject_counts[reason] += 1
                _record_no_trade_with_diagnostics(
                    executor=executor,
                    state=state,
                    reason=reason,
                    position=position,
                    diagnostics=diagnostics,
                )
                continue

            for reason in _rejected_add_reasons(state, position=position, before_fill_count=before_fill_count):
                reject_counts[reason] += 1
                _record_no_trade_with_diagnostics(
                    executor=executor,
                    state=state,
                    reason=reason,
                    position=position,
                    diagnostics=diagnostics,
                )

    summary = _summary_payload(
        feed_db=feed_path,
        run_dir=run_dir,
        results_db=results_db,
        run_id=run_id,
        positions=positions,
        snapshots_processed=snapshots_processed,
        fills=fills,
        reject_counts=reject_counts,
        market_ticker_filter=market_ticker,
        min_strike=min_strike,
        max_strike=max_strike,
        skipped_bad_strike_rows=skipped_bad_strike_rows,
        orderbook_snapshots_logged=orderbook_snapshots_logged,
        seed_fills=seed_fills,
        add_fills=add_fills,
        expiry_balance_fills=expiry_balance_fills,
        paired_costs=paired_costs,
        unpaired_counts_seen=unpaired_counts_seen,
        diagnostic_summary=diagnostics_tracker.summary(),
        buy_both_costs=buy_both_costs,
    )
    if settlement_price is not None:
        settlement = evaluate_settlement(
            positions=positions,
            strikes=strikes,
            settlement_price=settlement_price,
            settlement_source=settlement_source,
        )
        (run_dir / "settlement.json").write_text(
            json.dumps(settlement, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        summary.update(settlement)
    if settlements_csv is not None:
        settlement = evaluate_settlements_by_market(
            positions=positions,
            strikes=strikes,
            settlement_prices=_load_settlements_csv(Path(settlements_csv)),
            total_replayed_markets=len(positions),
        )
        _write_settlement_by_market(run_dir=run_dir, results_db=results_db, settlement=settlement)
        summary.update(settlement["aggregate"])
    if settlements_from_feed_db:
        settlement = evaluate_settlements_by_market(
            positions=positions,
            strikes=strikes,
            settlement_prices=_load_settlements_from_feed_db(feed_path),
            total_replayed_markets=len(positions),
        )
        _write_settlement_by_market(run_dir=run_dir, results_db=results_db, settlement=settlement)
        summary.update(settlement["aggregate"])
    (run_dir / "metrics.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (run_dir / "config.json").write_text(
        json.dumps(
            {
                "strategy": STRATEGY_NAME,
                "feed_db": str(feed_path),
                "from_ts": from_ts,
                "to_ts": to_ts,
                "market_ticker_filter": market_ticker,
                "min_strike": min_strike,
                "max_strike": max_strike,
                "volatility_window": volatility_window,
                "config": (config or HedgeVolatilityConfig()).__dict__,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return summary


def evaluate_settlement(
    *,
    positions: dict[str, HedgePosition],
    strikes: dict[str, float],
    settlement_price: float,
    settlement_source: SettlementSource,
) -> dict[str, Any]:
    """Evaluate final settlement geometry for replay-only hedge inventory."""

    price = float(settlement_price)
    total_yes_contracts = 0.0
    total_no_contracts = 0.0
    total_yes_cost = 0.0
    total_no_cost = 0.0
    gross_payout = 0.0
    paired_locked_edge = 0.0
    unpaired_directional_pnl = 0.0
    winning_sides: set[str] = set()

    for market_ticker, position in positions.items():
        if not position.fills:
            continue
        strike = float(strikes[market_ticker])
        winning_side = "yes" if price > strike else "no"
        winning_sides.add(winning_side)
        yes_contracts = position.yes_contracts
        no_contracts = position.no_contracts
        avg_yes = position.avg_yes_entry or 0.0
        avg_no = position.avg_no_entry or 0.0
        yes_cost = yes_contracts * avg_yes
        no_cost = no_contracts * avg_no
        total_yes_contracts += yes_contracts
        total_no_contracts += no_contracts
        total_yes_cost += yes_cost
        total_no_cost += no_cost
        gross_payout += yes_contracts if winning_side == "yes" else no_contracts

        paired = min(yes_contracts, no_contracts)
        paired_locked_edge += paired * (1.0 - avg_yes - avg_no)
        unpaired_yes = max(0.0, yes_contracts - no_contracts)
        unpaired_no = max(0.0, no_contracts - yes_contracts)
        if unpaired_yes:
            unpaired_directional_pnl += unpaired_yes * ((1.0 if winning_side == "yes" else 0.0) - avg_yes)
        if unpaired_no:
            unpaired_directional_pnl += unpaired_no * ((1.0 if winning_side == "no" else 0.0) - avg_no)

    total_cost = total_yes_cost + total_no_cost
    paired_contracts = min(total_yes_contracts, total_no_contracts)
    return {
        "settlement_price": _round(price),
        "settlement_source": settlement_source,
        "winning_side": winning_sides.pop() if len(winning_sides) == 1 else "mixed",
        "gross_payout": _round(gross_payout),
        "total_cost": _round(total_cost),
        "realized_pnl": _round(gross_payout - total_cost),
        "paired_contracts": _round(paired_contracts),
        "unpaired_yes_contracts": _round(max(0.0, total_yes_contracts - total_no_contracts)),
        "unpaired_no_contracts": _round(max(0.0, total_no_contracts - total_yes_contracts)),
        "paired_locked_edge": _round(paired_locked_edge),
        "unpaired_directional_pnl": _round(unpaired_directional_pnl),
    }


def evaluate_settlements_by_market(
    *,
    positions: dict[str, HedgePosition],
    strikes: dict[str, float],
    settlement_prices: dict[str, float],
    total_replayed_markets: int | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for market_ticker, position in sorted(positions.items()):
        if not position.fills or market_ticker not in settlement_prices:
            continue
        settlement_price = float(settlement_prices[market_ticker])
        strike = float(strikes[market_ticker])
        winning_side = "yes" if settlement_price > strike else "no"
        yes_qty = position.yes_contracts
        no_qty = position.no_contracts
        avg_yes = position.avg_yes_entry or 0.0
        avg_no = position.avg_no_entry or 0.0
        total_cost = yes_qty * avg_yes + no_qty * avg_no
        gross_payout = yes_qty if winning_side == "yes" else no_qty
        realized_pnl = gross_payout - total_cost
        paired_locked_edge = min(yes_qty, no_qty) * (1.0 - avg_yes - avg_no)
        unpaired_directional_pnl = realized_pnl - paired_locked_edge
        rows.append(
            {
                "market_ticker": market_ticker,
                "settlement_price": _round(settlement_price),
                "strike": _round(strike),
                "winning_side": winning_side,
                "yes_contracts": _round(yes_qty),
                "no_contracts": _round(no_qty),
                "avg_yes_entry": _round(position.avg_yes_entry),
                "avg_no_entry": _round(position.avg_no_entry),
                "gross_payout": _round(gross_payout),
                "total_cost": _round(total_cost),
                "realized_pnl": _round(realized_pnl),
                "paired_locked_edge": _round(paired_locked_edge),
                "unpaired_directional_pnl": _round(unpaired_directional_pnl),
            }
        )
    total_gross_payout = sum(float(row["gross_payout"] or 0.0) for row in rows)
    total_cost = sum(float(row["total_cost"] or 0.0) for row in rows)
    total_realized_pnl = sum(float(row["realized_pnl"] or 0.0) for row in rows)
    total_paired_locked_edge = sum(float(row["paired_locked_edge"] or 0.0) for row in rows)
    total_unpaired_directional_pnl = sum(float(row["unpaired_directional_pnl"] or 0.0) for row in rows)
    aggregate = {
        "settled_markets": len(rows),
        "unsettled_markets": max(0, (total_replayed_markets if total_replayed_markets is not None else len(positions)) - len(rows)),
        "total_gross_payout": _round(total_gross_payout),
        "total_cost": _round(total_cost),
        "total_realized_pnl": _round(total_realized_pnl),
        "total_paired_locked_edge": _round(total_paired_locked_edge),
        "total_unpaired_directional_pnl": _round(total_unpaired_directional_pnl),
        "winning_markets": sum(1 for row in rows if float(row["realized_pnl"] or 0.0) > 0.0),
        "losing_markets": sum(1 for row in rows if float(row["realized_pnl"] or 0.0) < 0.0),
        "avg_pnl_per_market": _round(total_realized_pnl / len(rows) if rows else None),
    }
    total_final_yes = sum(position.yes_contracts for position in positions.values())
    total_final_no = sum(position.no_contracts for position in positions.values())
    aggregate["final_unpaired_yes_contracts"] = _round(max(0.0, total_final_yes - total_final_no))
    aggregate["final_unpaired_no_contracts"] = _round(max(0.0, total_final_no - total_final_yes))
    return {"aggregate": aggregate, "markets": rows}


def _load_settlements_csv(path: Path) -> dict[str, float]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        required = {"market_ticker", "settlement_price"}
        if not required.issubset(reader.fieldnames or set()):
            raise ValueError("settlements CSV must contain market_ticker,settlement_price columns")
        return {str(row["market_ticker"]): float(row["settlement_price"]) for row in reader}
def _load_settlements_from_feed_db(feed_db: Path) -> dict[str, float]:
    with sqlite3.connect(f"file:{feed_db}?mode=ro", uri=True) as conn:
        return {
            str(row[0]): float(row[1])
            for row in conn.execute(
                """
                SELECT market_ticker, settlement_price
                FROM market_settlements
                WHERE settlement_price IS NOT NULL
                  AND source = 'kalshi_api'
                  AND status = 'settled_official'
                """
            )
        }


def _write_settlement_by_market(*, run_dir: Path, results_db: Path, settlement: dict[str, Any]) -> None:
    rows: list[dict[str, Any]] = settlement["markets"]
    aggregate: dict[str, Any] = settlement["aggregate"]
    (run_dir / "settlement.json").write_text(json.dumps(aggregate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    fieldnames = [
        "market_ticker",
        "settlement_price",
        "strike",
        "winning_side",
        "yes_contracts",
        "no_contracts",
        "avg_yes_entry",
        "avg_no_entry",
        "gross_payout",
        "total_cost",
        "realized_pnl",
        "paired_locked_edge",
        "unpaired_directional_pnl",
    ]
    with (run_dir / "settlement_by_market.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    with sqlite3.connect(results_db) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settlement_by_market (
                market_ticker TEXT PRIMARY KEY,
                settlement_price REAL NOT NULL,
                strike REAL NOT NULL,
                winning_side TEXT NOT NULL,
                yes_contracts REAL NOT NULL,
                no_contracts REAL NOT NULL,
                avg_yes_entry REAL,
                avg_no_entry REAL,
                gross_payout REAL NOT NULL,
                total_cost REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                paired_locked_edge REAL NOT NULL,
                unpaired_directional_pnl REAL NOT NULL
            )
            """
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO settlement_by_market (
                market_ticker, settlement_price, strike, winning_side,
                yes_contracts, no_contracts, avg_yes_entry, avg_no_entry,
                gross_payout, total_cost, realized_pnl, paired_locked_edge,
                unpaired_directional_pnl
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [tuple(row[name] for name in fieldnames) for row in rows],
        )


class RecentVolatility:
    def __init__(self, *, window: int) -> None:
        self.window = max(2, int(window))
        self.prices_by_market: dict[str, deque[float]] = {}

    def add(self, market_ticker: str, price: float) -> float | None:
        prices = self.prices_by_market.setdefault(market_ticker, deque(maxlen=self.window))
        prices.append(float(price))
        if len(prices) < 2:
            return None
        return max(prices) - min(prices)


class ReplayDiagnostics:
    def __init__(self, *, window: int = 30) -> None:
        self.window = max(2, int(window))
        self.prices_by_market: dict[str, deque[float]] = {}
        self.abs_distances_by_market: dict[str, deque[float]] = {}
        self.atrs_by_market: dict[str, deque[float]] = {}
        self.abs_distances: list[float] = []
        self.abs_distance_velocities: list[float] = []
        self.atrs: list[float] = []
        self.atr_expansions: list[float] = []

    def add(self, state: MarketState) -> dict[str, float | None]:
        market = state.orderbook.market_ticker
        price = float(state.price)
        abs_distance = abs(float(state.distance_from_strike))
        prices = self.prices_by_market.setdefault(market, deque(maxlen=self.window + 1))
        distances = self.abs_distances_by_market.setdefault(market, deque(maxlen=self.window + 1))
        atrs = self.atrs_by_market.setdefault(market, deque(maxlen=self.window + 1))

        previous_distance = distances[0] if len(distances) >= self.window else None
        prices.append(price)
        distances.append(abs_distance)

        atr_30s = _average_abs_change(prices)
        previous_atr = atrs[0] if len(atrs) >= self.window and atrs[0] not in {None, 0.0} else None
        atrs.append(atr_30s)
        distance_velocity = abs_distance - previous_distance if previous_distance is not None else None
        atr_expansion = atr_30s / previous_atr if previous_atr else None

        self.abs_distances.append(abs_distance)
        if distance_velocity is not None:
            self.abs_distance_velocities.append(abs(distance_velocity))
        if atr_30s is not None:
            self.atrs.append(atr_30s)
        if atr_expansion is not None:
            self.atr_expansions.append(atr_expansion)

        return {
            "time_to_expiry": state.seconds_to_close,
            "abs_slope_30s": abs(state.slope_30s or 0.0),
            "abs_distance_from_strike": abs_distance,
            "distance_velocity_30s": distance_velocity,
            "abs_distance_velocity_30s": abs(distance_velocity) if distance_velocity is not None else None,
            "atr_30s": atr_30s,
            "atr_expansion_30s": atr_expansion,
        }

    def summary(self) -> dict[str, float | None]:
        return {
            "max_abs_distance_from_strike": _round(max(self.abs_distances) if self.abs_distances else None),
            "max_abs_distance_velocity_30s": _round(
                max(self.abs_distance_velocities) if self.abs_distance_velocities else None
            ),
            "max_atr_30s": _round(max(self.atrs) if self.atrs else None),
            "max_atr_expansion_30s": _round(max(self.atr_expansions) if self.atr_expansions else None),
            "avg_atr_30s": _round(sum(self.atrs) / len(self.atrs) if self.atrs else None),
        }


def _average_abs_change(prices: deque[float]) -> float | None:
    if len(prices) < 2:
        return None
    changes = [abs(prices[index] - prices[index - 1]) for index in range(1, len(prices))]
    return sum(changes) / len(changes)


def _load_snapshot_rows(
    feed_db: Path,
    *,
    from_ts: str | None,
    to_ts: str | None,
    market_ticker: str | None,
    min_strike: float,
    max_strike: float,
) -> Iterable[sqlite3.Row]:
    if not feed_db.exists():
        raise FileNotFoundError(f"feed DB not found: {feed_db}")
    where: list[str] = ["strike BETWEEN ? AND ?"]
    params: list[Any] = [float(min_strike), float(max_strike)]
    if from_ts:
        where.append("ts >= ?")
        params.append(from_ts)
    if to_ts:
        where.append("ts <= ?")
        params.append(to_ts)
    if market_ticker:
        where.append("market_ticker = ?")
        params.append(market_ticker)
    sql = "SELECT * FROM realtime_snapshots_1s WHERE " + " AND ".join(where)
    sql += " ORDER BY ts ASC, market_ticker ASC"
    with sqlite3.connect(f"file:{feed_db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        yield from conn.execute(sql, params)


def _count_bad_strike_rows(
    feed_db: Path,
    *,
    from_ts: str | None,
    to_ts: str | None,
    market_ticker: str | None,
    min_strike: float,
    max_strike: float,
) -> int:
    if not feed_db.exists():
        raise FileNotFoundError(f"feed DB not found: {feed_db}")
    where: list[str] = ["(strike < ? OR strike > ?)"]
    params: list[Any] = [float(min_strike), float(max_strike)]
    if from_ts:
        where.append("ts >= ?")
        params.append(from_ts)
    if to_ts:
        where.append("ts <= ?")
        params.append(to_ts)
    if market_ticker:
        where.append("market_ticker = ?")
        params.append(market_ticker)
    sql = "SELECT COUNT(*) FROM realtime_snapshots_1s WHERE " + " AND ".join(where)
    with sqlite3.connect(f"file:{feed_db}?mode=ro", uri=True) as conn:
        return int(conn.execute(sql, params).fetchone()[0])


def _state_from_row(row: sqlite3.Row) -> MarketState:
    ts = _parse_dt(row["ts"])
    close_time = _parse_dt(row["market_close_time"])
    open_time = _parse_dt(row["market_open_time"]) if _has_column(row, "market_open_time") and row["market_open_time"] else None
    raw = _json_or_empty(row["raw_json"] if _has_column(row, "raw_json") else None)
    raw.update(
        {
            "market_open_time": open_time.isoformat() if open_time else None,
            "market_close_time": close_time.isoformat(),
            "strike": float(row["strike"]),
        }
    )
    market_ticker = str(row["market_ticker"])
    contract = ContractWindow(
        ticker=market_ticker,
        strike=float(row["strike"]),
        close_time=close_time,
        open_time=open_time,
    )
    return MarketState(
        tick=Tick(
            ts=ts,
            price=float(row["btc_price"]),
            source="hedge_volatility_v0_replay",
            symbol="BTC-USD",
            raw=raw,
        ),
        orderbook=OrderBookSnapshot(
            ts=ts,
            market_ticker=market_ticker,
            yes_bid=_optional_float(row, "yes_bid"),
            yes_ask=_optional_float(row, "yes_ask"),
            no_bid=_optional_float(row, "no_bid"),
            no_ask=_optional_float(row, "no_ask"),
            sequence=_optional_int(row, "orderbook_sequence"),
            raw=raw,
        ),
        contract=contract,
        slope_30s=_optional_float(row, "slope_30s"),
    )


def _infer_reject_reason(
    state: MarketState,
    *,
    position: HedgePosition,
    config: HedgeVolatilityConfig,
    recent_volatility: float | None,
) -> str:
    slope = state.slope_30s or 0.0
    if slope == 0.0:
        return "flat_trend"
    if abs(slope) < config.min_abs_slope:
        return "slope_too_small"
    if abs(state.distance_from_strike) < config.min_distance_from_strike:
        return "distance_too_small"
    if state.seconds_to_close < config.min_seconds_to_expiry:
        return "too_close_to_expiry"
    if state.seconds_to_close > config.max_seconds_to_expiry:
        return "too_early_for_contract"
    if recent_volatility is not None and recent_volatility < config.min_recent_volatility:
        return "recent_volatility_too_small"
    if state.orderbook.yes_ask is None or state.orderbook.no_ask is None:
        return "missing_executable_ask"
    if state.orderbook.yes_ask > config.max_leg_ask or state.orderbook.no_ask > config.max_leg_ask:
        return "leg_ask_too_high"
    if position.yes_contracts == 0.0 and position.no_contracts == 0.0:
        if state.orderbook.yes_ask + state.orderbook.no_ask > config.seed_max_pair_cost:
            return "seed_pair_cost_too_high"
    return "no_decision"


def _rejected_add_reasons(
    state: MarketState,
    *,
    position: HedgePosition,
    before_fill_count: int,
) -> list[str]:
    if before_fill_count == 0:
        return []
    reasons: list[str] = []
    if state.orderbook.yes_ask is not None and position.avg_yes_entry is not None and state.orderbook.yes_ask >= position.avg_yes_entry:
        reasons.append("yes_price_not_improved")
    if state.orderbook.no_ask is not None and position.avg_no_entry is not None and state.orderbook.no_ask >= position.avg_no_entry:
        reasons.append("no_price_not_improved")
    return reasons


def _record_no_trade_with_diagnostics(
    *,
    executor: HedgePaperExecutor,
    state: MarketState,
    reason: str,
    position: HedgePosition,
    diagnostics: dict[str, float | None],
) -> None:
    executor.record_no_trade(state=state, reason=reason, position=position)
    raw_json = json.dumps(
        {
            "btc_price": state.price,
            "strike": state.strike,
            "distance_from_strike": state.distance_from_strike,
            "seconds_to_close": state.seconds_to_close,
            "slope_30s": state.slope_30s,
            "yes_ask": state.orderbook.yes_ask,
            "no_ask": state.orderbook.no_ask,
            **diagnostics,
        },
        sort_keys=True,
    )
    executor.conn.execute(
        """
        UPDATE hedge_decisions
        SET raw_json = ?
        WHERE id = last_insert_rowid()
        """,
        (raw_json,),
    )


def _initialize_orderbook_snapshot_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hedge_orderbook_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            market_ticker TEXT NOT NULL,
            btc_price REAL NOT NULL,
            strike REAL NOT NULL,
            seconds_to_close REAL NOT NULL,
            slope_30s REAL,
            recent_volatility REAL,
            yes_bid REAL,
            yes_ask REAL,
            no_bid REAL,
            no_ask REAL,
            buy_both_cost REAL,
            sell_both_credit REAL,
            distance_from_strike REAL NOT NULL,
            time_to_expiry REAL NOT NULL,
            abs_slope_30s REAL NOT NULL,
            abs_distance_from_strike REAL NOT NULL,
            distance_velocity_30s REAL,
            abs_distance_velocity_30s REAL,
            atr_30s REAL,
            atr_expansion_30s REAL,
            raw_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_hedge_orderbook_snapshots_market_ts
            ON hedge_orderbook_snapshots(market_ticker, ts)
        """
    )


def _orderbook_snapshot_params(
    state: MarketState,
    *,
    recent_volatility: float | None,
    diagnostics: dict[str, float | None],
) -> tuple[Any, ...]:
    raw = dict(state.orderbook.raw or state.tick.raw or {})
    raw.update(diagnostics)
    return (
        state.tick.ts.isoformat(),
        state.orderbook.market_ticker,
        state.price,
        state.strike,
        state.seconds_to_close,
        state.slope_30s,
        recent_volatility,
        state.orderbook.yes_bid,
        state.orderbook.yes_ask,
        state.orderbook.no_bid,
        state.orderbook.no_ask,
        _buy_both_cost(state),
        _sell_both_credit(state),
        state.distance_from_strike,
        diagnostics["time_to_expiry"],
        diagnostics["abs_slope_30s"],
        diagnostics["abs_distance_from_strike"],
        diagnostics["distance_velocity_30s"],
        diagnostics["abs_distance_velocity_30s"],
        diagnostics["atr_30s"],
        diagnostics["atr_expansion_30s"],
        json.dumps(raw, sort_keys=True),
    )


def _buy_both_cost(state: MarketState) -> float | None:
    if state.orderbook.yes_ask is None or state.orderbook.no_ask is None:
        return None
    return state.orderbook.yes_ask + state.orderbook.no_ask


def _sell_both_credit(state: MarketState) -> float | None:
    if state.orderbook.yes_bid is None or state.orderbook.no_bid is None:
        return None
    return state.orderbook.yes_bid + state.orderbook.no_bid


def _summary_payload(
    *,
    feed_db: Path,
    run_dir: Path,
    results_db: Path,
    run_id: str,
    positions: dict[str, HedgePosition],
    snapshots_processed: int,
    fills: int,
    reject_counts: Counter[str],
    market_ticker_filter: str | None,
    min_strike: float,
    max_strike: float,
    skipped_bad_strike_rows: int,
    orderbook_snapshots_logged: int,
    seed_fills: int,
    add_fills: int,
    expiry_balance_fills: int,
    paired_costs: list[float],
    unpaired_counts_seen: list[float],
    diagnostic_summary: dict[str, float | None],
    buy_both_costs: list[float],
) -> dict[str, Any]:
    final_position = _latest_position(positions.values())
    final_yes = final_position.yes_contracts if final_position else 0.0
    final_no = final_position.no_contracts if final_position else 0.0
    final_unpaired_yes = max(0.0, final_yes - final_no)
    final_unpaired_no = max(0.0, final_no - final_yes)
    return {
        "strategy": STRATEGY_NAME,
        "run_id": run_id,
        "feed_db": str(feed_db),
        "run_dir": str(run_dir),
        "results_db": str(results_db),
        "market_ticker_filter": market_ticker_filter,
        "min_strike": _round(min_strike),
        "max_strike": _round(max_strike),
        "skipped_bad_strike_rows": skipped_bad_strike_rows,
        "orderbook_snapshots_logged": orderbook_snapshots_logged,
        "seed_fills": seed_fills,
        "add_fills": add_fills,
        "expiry_balance_fills": expiry_balance_fills,
        "final_unpaired_yes_contracts": _round(final_unpaired_yes),
        "final_unpaired_no_contracts": _round(final_unpaired_no),
        "max_unpaired_contracts_seen": _round(max(unpaired_counts_seen) if unpaired_counts_seen else 0.0),
        "imbalance_rejects": reject_counts.get("would_increase_directional_imbalance", 0)
        + reject_counts.get("max_imbalance_ratio_exceeded", 0)
        + reject_counts.get("max_unpaired_contracts_exceeded", 0),
        "best_paired_cost": _round(min(paired_costs) if paired_costs else None),
        "worst_paired_cost": _round(max(paired_costs) if paired_costs else None),
        "final_paired_cost": _round(final_position.combined_average_cost if final_position else None),
        "best_edge": _round(1.0 - min(paired_costs) if paired_costs else None),
        "final_edge": _round(final_position.locked_edge_per_pair if final_position else None),
        "buy_both_cost_min": _round(min(buy_both_costs) if buy_both_costs else None),
        "buy_both_cost_max": _round(max(buy_both_costs) if buy_both_costs else None),
        "buy_both_cost_mean": _round(sum(buy_both_costs) / len(buy_both_costs) if buy_both_costs else None),
        **diagnostic_summary,
        "markets_processed": len(positions),
        "snapshots_processed": snapshots_processed,
        "fills": fills,
        "final_yes_contracts": _round(final_yes),
        "final_no_contracts": _round(final_no),
        "avg_yes_entry": _round(final_position.avg_yes_entry if final_position else None),
        "avg_no_entry": _round(final_position.avg_no_entry if final_position else None),
        "combined_average_cost": _round(final_position.combined_average_cost if final_position else None),
        "locked_edge_per_pair": _round(final_position.locked_edge_per_pair if final_position else None),
        "reject_counts_by_reason": dict(sorted(reject_counts.items())),
    }


def _latest_position(positions: Iterable[HedgePosition]) -> HedgePosition | None:
    non_empty = [position for position in positions if position.fills]
    if not non_empty:
        return None
    return max(non_empty, key=lambda position: max(fill.ts for fill in position.fills))


def _format_summary(summary: dict[str, Any]) -> str:
    rejects = ",".join(
        f"{reason}:{count}" for reason, count in summary["reject_counts_by_reason"].items()
    ) or "none"
    return (
        "hedge_volatility_v0_replay "
        f"markets={summary['markets_processed']} "
        f"snapshots={summary['snapshots_processed']} "
        f"fills={summary['fills']} "
        f"final_yes={summary['final_yes_contracts']} "
        f"final_no={summary['final_no_contracts']} "
        f"avg_yes={summary['avg_yes_entry']} "
        f"avg_no={summary['avg_no_entry']} "
        f"combined_avg_cost={summary['combined_average_cost']} "
        f"locked_edge_per_pair={summary['locked_edge_per_pair']} "
        f"rejects={rejects} "
        f"run_dir={summary['run_dir']}"
    )


def _round(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _has_column(row: sqlite3.Row, name: str) -> bool:
    return name in row.keys()


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
