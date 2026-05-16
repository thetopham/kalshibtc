from __future__ import annotations

import argparse
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
    parser.add_argument("--max-projected-pair-cost", type=float, default=0.95)
    parser.add_argument("--min-abs-slope", type=float, default=0.0)
    parser.add_argument("--min-recent-volatility", type=float, default=0.0)
    parser.add_argument("--min-distance-from-strike", type=float, default=0.0)
    parser.add_argument("--min-seconds-to-expiry", type=float, default=45.0)
    parser.add_argument("--max-seconds-to-expiry", type=float, default=14.5 * 60.0)
    parser.add_argument("--volatility-window", type=int, default=30)
    parser.add_argument("--settlement-price", type=float, default=None)
    parser.add_argument(
        "--settlement-source",
        choices=["manual", "kalshi", "coinbase_1m_avg", "chainlink_1m_avg"],
        default="manual",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON summary.")
    parser.add_argument("--overwrite", action="store_true", help="Allow replacing an existing run directory.")
    args = parser.parse_args(argv)

    feed_db = resolve_feed_db(args.feed_db)
    runs_dir = resolve_runs_dir(args.runs_dir)
    run_id = args.run_id or datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    config = HedgeVolatilityConfig(
        max_projected_pair_cost=args.max_projected_pair_cost,
        min_abs_slope=args.min_abs_slope,
        min_recent_volatility=args.min_recent_volatility,
        min_distance_from_strike=args.min_distance_from_strike,
        min_seconds_to_expiry=args.min_seconds_to_expiry,
        max_seconds_to_expiry=args.max_seconds_to_expiry,
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
    settlement_source: SettlementSource = "manual",
    overwrite: bool = False,
) -> dict[str, Any]:
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
    volatility_tracker = RecentVolatility(window=volatility_window)

    with HedgePaperExecutor(results_db=results_db, strategy_name=strategy.name) as executor:
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
            else:
                reason = _infer_reject_reason(
                    state,
                    position=position,
                    config=strategy.config,
                    recent_volatility=recent_volatility,
                )
                reject_counts[reason] += 1
                executor.record_no_trade(state=state, reason=reason, position=position)
                continue

            for reason in _rejected_add_reasons(state, position=position, before_fill_count=before_fill_count):
                reject_counts[reason] += 1
                executor.record_no_trade(state=state, reason=reason, position=position)

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
        if state.orderbook.yes_ask + state.orderbook.no_ask > config.max_projected_pair_cost:
            return "projected_combined_cost_too_high"
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
) -> dict[str, Any]:
    final_position = _latest_position(positions.values())
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
        "markets_processed": len(positions),
        "snapshots_processed": snapshots_processed,
        "fills": fills,
        "final_yes_contracts": _round(final_position.yes_contracts if final_position else 0.0),
        "final_no_contracts": _round(final_position.no_contracts if final_position else 0.0),
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
