from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ..execution.paper_inventory import InventoryFill, PaperInventoryPosition

Side = Literal["YES", "NO"]


@dataclass(frozen=True)
class Snapshot:
    ts: datetime
    market_ticker: str
    market_open_time: datetime | None
    market_close_time: datetime
    btc_price: float
    strike: float
    distance_from_strike: float
    seconds_to_close: float
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    slope: float | None = None


@dataclass(frozen=True)
class DynamicHedgeConfig:
    initial_shares: float = 100.0
    hedge_shares: float = 25.0
    max_total_shares: float = 200.0
    max_total_cost: float = 200.0
    required_edge: float = 0.03
    initial_entry_min_seconds_to_close: float = 12 * 60.0
    initial_entry_max_seconds_to_close: float = 14 * 60.0
    final_entry_cutoff_seconds: float = 90.0
    final_hedge_cutoff_seconds: float = 15.0
    edge_persistence_snapshots: int = 2
    cooldown_seconds: float = 3.0
    slippage: float = 0.01
    fee_per_share: float = 0.0
    allow_overhedge: bool = False
    slope_column_fallback: str = "slope_30s"


@dataclass(frozen=True)
class Opportunity:
    ts: datetime
    market_ticker: str
    initial_side: Side | None
    yes_ask_plus_no_ask: float | None
    yes_bid_plus_no_bid: float | None
    opposite_hedge_ask: float | None
    combined_basis_after_hedge: float | None
    locked_edge_after_hedge: float | None
    temporal_combined_basis: float | None
    temporal_edge: float | None
    mark_value: float | None
    max_payout_if_yes: float
    max_payout_if_no: float
    worst_case_settlement_value: float
    worst_case_pnl: float
    distance_from_strike: float
    seconds_to_close: float


@dataclass(frozen=True)
class Decision:
    snapshot: Snapshot
    position: PaperInventoryPosition
    fill: InventoryFill | None = None
    reason: str = ""
    opportunity: Opportunity | None = None


@dataclass
class TemporalBasisCompression:
    market_ticker: str
    initial_side: Side | None = None
    initial_entry_price: float | None = None
    initial_entry_ts: datetime | None = None
    best_future_opposite_ask: float | None = None
    best_combined_basis_seen: float | None = None
    best_locked_edge_seen: float | None = None
    time_to_expiry_at_best: float | None = None
    distance_from_strike_at_best: float | None = None
    btc_price_at_best: float | None = None
    ts_at_best: datetime | None = None
    observations: list[dict[str, Any]] = field(default_factory=list)

    def set_initial(self, *, side: Side, price: float, ts: datetime) -> None:
        if self.initial_side is None:
            self.initial_side = side
            self.initial_entry_price = price
            self.initial_entry_ts = ts

    def observe(self, snapshot: Snapshot) -> dict[str, Any] | None:
        if self.initial_side is None or self.initial_entry_price is None:
            return None
        opposite_ask = snapshot.no_ask if self.initial_side == "YES" else snapshot.yes_ask
        if opposite_ask is None or opposite_ask <= 0 or opposite_ask > 1:
            return None
        combined_basis = self.initial_entry_price + opposite_ask
        temporal_edge = 1.0 - combined_basis
        row = {
            "ts": snapshot.ts.isoformat(),
            "market_ticker": snapshot.market_ticker,
            "initial_side": self.initial_side,
            "initial_entry_price": self.initial_entry_price,
            "future_opposite_ask": opposite_ask,
            "combined_basis": combined_basis,
            "temporal_edge": temporal_edge,
            "seconds_to_close": snapshot.seconds_to_close,
            "distance_from_strike": snapshot.distance_from_strike,
            "btc_price": snapshot.btc_price,
            "abs_distance_from_strike": abs(snapshot.distance_from_strike),
        }
        self.observations.append(row)
        if self.best_combined_basis_seen is None or combined_basis < self.best_combined_basis_seen:
            self.best_future_opposite_ask = opposite_ask
            self.best_combined_basis_seen = combined_basis
            self.best_locked_edge_seen = temporal_edge
            self.time_to_expiry_at_best = snapshot.seconds_to_close
            self.distance_from_strike_at_best = snapshot.distance_from_strike
            self.btc_price_at_best = snapshot.btc_price
            self.ts_at_best = snapshot.ts
        return row

    def summary_dict(self) -> dict[str, Any]:
        return {
            "market_ticker": self.market_ticker,
            "initial_side": self.initial_side or "",
            "initial_entry_price": self.initial_entry_price,
            "initial_entry_ts": self.initial_entry_ts.isoformat() if self.initial_entry_ts else "",
            "best_future_opposite_ask": self.best_future_opposite_ask,
            "best_combined_basis_seen": self.best_combined_basis_seen,
            "best_locked_edge_seen": self.best_locked_edge_seen,
            "temporal_edge": self.best_locked_edge_seen,
            "time_to_expiry_at_best": self.time_to_expiry_at_best,
            "distance_from_strike_at_best": self.distance_from_strike_at_best,
            "btc_price_at_best": self.btc_price_at_best,
            "ts_at_best": self.ts_at_best.isoformat() if self.ts_at_best else "",
            "observations": len(self.observations),
        }


class DynamicComplementHedgeBot:
    name = "dynamic_complement_hedge"

    def __init__(self, config: DynamicHedgeConfig | None = None) -> None:
        self.config = config or DynamicHedgeConfig()
        self.positions: dict[str, PaperInventoryPosition] = {}
        self.temporal: dict[str, TemporalBasisCompression] = {}
        self._edge_persistence: dict[tuple[str, Side], int] = defaultdict(int)
        self.opportunities: list[Opportunity] = []

    def on_snapshot(self, snapshot: Snapshot, *, scan_only: bool = False) -> Decision:
        position = self.positions.setdefault(
            snapshot.market_ticker, PaperInventoryPosition(snapshot.market_ticker)
        )
        temporal = self.temporal.setdefault(
            snapshot.market_ticker, TemporalBasisCompression(snapshot.market_ticker)
        )
        if not is_valid_book(snapshot):
            return Decision(snapshot=snapshot, position=position, reason="invalid_book")

        if scan_only:
            self._maybe_set_scan_initial(snapshot, temporal)
            temporal.observe(snapshot)
            opportunity = self._opportunity(snapshot, position, temporal)
            self.opportunities.append(opportunity)
            return Decision(snapshot=snapshot, position=position, reason="scan_only", opportunity=opportunity)

        fill = self._maybe_initial_entry(snapshot, position)
        if fill is not None:
            temporal.set_initial(side=fill.side, price=fill.price, ts=fill.ts)
            temporal.observe(snapshot)
            opportunity = self._opportunity(snapshot, position, temporal)
            self.opportunities.append(opportunity)
            return Decision(snapshot=snapshot, position=position, fill=fill, reason="initial_entry", opportunity=opportunity)

        temporal.observe(snapshot)
        fill = self._maybe_hedge(snapshot, position)
        opportunity = self._opportunity(snapshot, position, temporal)
        self.opportunities.append(opportunity)
        return Decision(
            snapshot=snapshot,
            position=position,
            fill=fill,
            reason="hedge" if fill else "hold",
            opportunity=opportunity,
        )

    def _maybe_set_scan_initial(self, snapshot: Snapshot, temporal: TemporalBasisCompression) -> None:
        if temporal.initial_side is not None:
            return
        side = initial_entry_side(snapshot)
        if side is None:
            return
        ask = snapshot.yes_ask if side == "YES" else snapshot.no_ask
        if ask is not None:
            temporal.set_initial(side=side, price=ask + self.config.slippage, ts=snapshot.ts)

    def _maybe_initial_entry(self, snapshot: Snapshot, position: PaperInventoryPosition) -> InventoryFill | None:
        if position.initial_entry_done:
            return None
        if snapshot.seconds_to_close < self.config.final_entry_cutoff_seconds:
            return None
        if not (
            self.config.initial_entry_min_seconds_to_close
            <= snapshot.seconds_to_close
            <= self.config.initial_entry_max_seconds_to_close
        ):
            return None
        side = initial_entry_side(snapshot)
        if side is None:
            return None
        ask = snapshot.yes_ask if side == "YES" else snapshot.no_ask
        assert ask is not None
        return self._fill(position, side=side, ask=ask, shares=self.config.initial_shares, ts=snapshot.ts, reason="initial_directional_leg")

    def _maybe_hedge(self, snapshot: Snapshot, position: PaperInventoryPosition) -> InventoryFill | None:
        if not position.initial_entry_done:
            return None
        if snapshot.seconds_to_close < self.config.final_hedge_cutoff_seconds:
            return None
        if position.last_fill_ts is not None:
            elapsed = (snapshot.ts - position.last_fill_ts).total_seconds()
            if elapsed < self.config.cooldown_seconds:
                return None

        if position.yes_shares > position.no_shares:
            side: Side = "NO"
            ask = snapshot.no_ask
            max_hedge = position.yes_shares - position.no_shares if not self.config.allow_overhedge else self.config.max_total_shares - position.total_shares
        elif position.no_shares > position.yes_shares:
            side = "YES"
            ask = snapshot.yes_ask
            max_hedge = position.no_shares - position.yes_shares if not self.config.allow_overhedge else self.config.max_total_shares - position.total_shares
        else:
            return None
        if ask is None or max_hedge <= 0:
            return None
        shares = min(self.config.hedge_shares, max_hedge, self.config.max_total_shares - position.total_shares)
        if shares <= 0:
            return None
        price = ask + self.config.slippage
        fee = shares * self.config.fee_per_share
        if position.total_cost + price * shares + fee > self.config.max_total_cost:
            return None
        if not hedge_improves_locked_basis(position, side=side, price=price, shares=shares, fee=fee, required_edge=self.config.required_edge):
            self._edge_persistence[(position.market_ticker, side)] = 0
            return None
        key = (position.market_ticker, side)
        self._edge_persistence[key] += 1
        if self._edge_persistence[key] < self.config.edge_persistence_snapshots:
            return None
        self._edge_persistence[key] = 0
        return self._fill(position, side=side, ask=ask, shares=shares, ts=snapshot.ts, reason="partial_opposite_hedge")

    def _fill(
        self,
        position: PaperInventoryPosition,
        *,
        side: Side,
        ask: float,
        shares: float,
        ts: datetime,
        reason: str,
    ) -> InventoryFill:
        price = ask + self.config.slippage
        fee = shares * self.config.fee_per_share
        return position.add_fill(side=side, price=price, shares=shares, ts=ts, fee=fee, reason=reason)

    def _opportunity(
        self,
        snapshot: Snapshot,
        position: PaperInventoryPosition,
        temporal: TemporalBasisCompression,
    ) -> Opportunity:
        yes_ask_plus_no_ask = _sum_optional(snapshot.yes_ask, snapshot.no_ask)
        yes_bid_plus_no_bid = _sum_optional(snapshot.yes_bid, snapshot.no_bid)
        opposite_side: Side | None = None
        opposite_ask: float | None = None
        if position.yes_shares > position.no_shares:
            opposite_side = "NO"
            opposite_ask = snapshot.no_ask
        elif position.no_shares > position.yes_shares:
            opposite_side = "YES"
            opposite_ask = snapshot.yes_ask
        combined_basis = None
        locked_edge = None
        if opposite_side and opposite_ask is not None:
            shares = min(self.config.hedge_shares, abs(position.directional_exposure))
            price = opposite_ask + self.config.slippage
            combined_basis = combined_basis_after_fill(position, side=opposite_side, price=price, shares=shares)
            locked_edge = locked_edge_after_fill(position, side=opposite_side, price=price, shares=shares)
        mark = position.mark_value(yes_bid=snapshot.yes_bid, no_bid=snapshot.no_bid)
        temporal_basis = temporal.observations[-1]["combined_basis"] if temporal.observations else None
        temporal_edge = temporal.observations[-1]["temporal_edge"] if temporal.observations else None
        return Opportunity(
            ts=snapshot.ts,
            market_ticker=snapshot.market_ticker,
            initial_side=position.initial_side or temporal.initial_side,
            yes_ask_plus_no_ask=yes_ask_plus_no_ask,
            yes_bid_plus_no_bid=yes_bid_plus_no_bid,
            opposite_hedge_ask=opposite_ask,
            combined_basis_after_hedge=combined_basis,
            locked_edge_after_hedge=locked_edge,
            temporal_combined_basis=temporal_basis,
            temporal_edge=temporal_edge,
            mark_value=mark,
            max_payout_if_yes=position.max_payout_if_yes,
            max_payout_if_no=position.max_payout_if_no,
            worst_case_settlement_value=position.worst_case_settlement_value,
            worst_case_pnl=position.worst_case_pnl(),
            distance_from_strike=snapshot.distance_from_strike,
            seconds_to_close=snapshot.seconds_to_close,
        )


def initial_entry_side(snapshot: Snapshot) -> Side | None:
    if snapshot.slope is None:
        return None
    if snapshot.btc_price > snapshot.strike and snapshot.slope > 0:
        return "YES"
    if snapshot.btc_price < snapshot.strike and snapshot.slope < 0:
        return "NO"
    return None


def is_valid_book(snapshot: Snapshot) -> bool:
    prices = (snapshot.yes_bid, snapshot.yes_ask, snapshot.no_bid, snapshot.no_ask)
    if any(price is None for price in prices):
        return False
    yes_bid, yes_ask, no_bid, no_ask = prices
    assert yes_bid is not None and yes_ask is not None and no_bid is not None and no_ask is not None
    if any(price <= 0 or price > 1 for price in (yes_bid, yes_ask, no_bid, no_ask)):
        return False
    if yes_bid > yes_ask or no_bid > no_ask:
        return False
    if yes_bid + no_bid > 1.0:
        return False
    return True


def combined_basis_after_fill(position: PaperInventoryPosition, *, side: Side, price: float, shares: float, fee: float = 0.0) -> float:
    yes_shares = position.yes_shares + (shares if side == "YES" else 0.0)
    no_shares = position.no_shares + (shares if side == "NO" else 0.0)
    if yes_shares <= 0 or no_shares <= 0:
        return 999.0
    yes_cost = position.yes_cost + (price * shares + fee if side == "YES" else 0.0)
    no_cost = position.no_cost + (price * shares + fee if side == "NO" else 0.0)
    yes_avg = yes_cost / yes_shares
    no_avg = no_cost / no_shares
    return yes_avg + no_avg


def locked_edge_after_fill(position: PaperInventoryPosition, *, side: Side, price: float, shares: float, fee: float = 0.0) -> float:
    yes_shares = position.yes_shares + (shares if side == "YES" else 0.0)
    no_shares = position.no_shares + (shares if side == "NO" else 0.0)
    matched = min(yes_shares, no_shares)
    if matched <= 0:
        return 0.0
    basis = combined_basis_after_fill(position, side=side, price=price, shares=shares, fee=fee)
    return matched * (1.0 - basis)


def hedge_improves_locked_basis(
    position: PaperInventoryPosition,
    *,
    side: Side,
    price: float,
    shares: float,
    fee: float = 0.0,
    required_edge: float,
) -> bool:
    basis = combined_basis_after_fill(position, side=side, price=price, shares=shares, fee=fee)
    return basis <= 1.0 - required_edge


def replay_feed_db(
    feed_db: str | Path,
    *,
    out_dir: str | Path | None = None,
    config: DynamicHedgeConfig | None = None,
    scan_only: bool = False,
    from_ts: str | None = None,
    to_ts: str | None = None,
) -> dict[str, Any]:
    feed_path = Path(feed_db)
    cfg = config or DynamicHedgeConfig()
    bot = DynamicComplementHedgeBot(cfg)
    rows = load_snapshots(feed_path, from_ts=from_ts, to_ts=to_ts)
    market_rows: dict[str, list[Snapshot]] = defaultdict(list)
    for row in rows:
        market_rows[row.market_ticker].append(row)
    for ticker in sorted(market_rows):
        for snapshot in sorted(market_rows[ticker], key=lambda item: item.ts):
            bot.on_snapshot(snapshot, scan_only=scan_only)
    results: list[dict[str, Any]] = []
    for ticker, snapshots in market_rows.items():
        final = max(snapshots, key=lambda item: item.ts)
        winner: Literal["YES", "NO"] = "YES" if final.btc_price > final.strike else "NO"
        position = bot.positions.get(ticker, PaperInventoryPosition(ticker))
        if position.total_shares > 0:
            position.settle(winner)
        temporal = bot.temporal.get(ticker, TemporalBasisCompression(ticker))
        row = position.summary_dict()
        row.update(temporal.summary_dict())
        row["final_winner"] = winner
        row["final_btc_price"] = final.btc_price
        row["settlement_value"] = position.settlement_value
        row["pnl"] = position.realized_pnl
        results.append(row)
    summary = summarize_results(results, opportunities=bot.opportunities, scan_only=scan_only)
    if out_dir is not None:
        write_replay_outputs(Path(out_dir), summary=summary, results=results, opportunities=bot.opportunities, temporal=bot.temporal)
    return summary


def load_snapshots(feed_db: Path, *, from_ts: str | None = None, to_ts: str | None = None) -> list[Snapshot]:
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
    sql += " ORDER BY market_ticker ASC, ts ASC"
    with sqlite3.connect(f"file:{feed_db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [_snapshot_from_row(row) for row in conn.execute(sql, params)]


def summarize_results(
    results: list[dict[str, Any]],
    *,
    opportunities: list[Opportunity],
    scan_only: bool,
) -> dict[str, Any]:
    traded = [row for row in results if float(row.get("total_cost") or 0.0) > 0]
    hedged = [row for row in traded if float(row.get("matched_shares") or 0.0) > 0]
    pnls = [float(row.get("pnl") or 0.0) for row in traded]
    locked_edges = [float(row.get("locked_edge") or 0.0) for row in hedged]
    bases = [float(row["best_combined_basis_seen"]) for row in results if row.get("best_combined_basis_seen") is not None]
    compression_thresholds = {str(threshold): sum(1 for basis in bases if basis < threshold) for threshold in (0.99, 0.95, 0.92, 0.90, 0.85)}
    exploitable_count = compression_thresholds["0.95"]
    structural_note = (
        "temporal complement accumulation shows repeated sub-0.95 compression in replay; investigate fill realism, fees, latency, and per-market capacity"
        if results and exploitable_count / max(len(results), 1) >= 0.2
        else "temporal complement accumulation did not clear a broad structural-exploitability threshold in this replay sample"
    )
    return {
        "strategy": "dynamic_complement_hedge",
        "mode": "scan_only" if scan_only else "paper_trade",
        "paper_only": True,
        "markets_tested": len(results),
        "markets_traded": len(traded),
        "markets_hedged": len(hedged),
        "total_paper_pnl": round(sum(pnls), 6),
        "average_pnl_per_traded_market": round(sum(pnls) / len(pnls), 6) if pnls else 0.0,
        "win_rate": (sum(1 for pnl in pnls if pnl > 0) / len(pnls)) if pnls else 0.0,
        "worst_loss": min(pnls) if pnls else 0.0,
        "best_win": max(pnls) if pnls else 0.0,
        "average_locked_edge": sum(locked_edges) / len(locked_edges) if locked_edges else 0.0,
        "fills": sum(len(row.get("fills") or []) for row in results),
        "temporal_basis_compression": {
            "markets_with_initial_entry": sum(1 for row in results if row.get("initial_entry_price") is not None),
            "count_best_combined_basis_lt": compression_thresholds,
            "best_basis_min": min(bases) if bases else None,
            "best_basis_avg": sum(bases) / len(bases) if bases else None,
            "structural_exploitability_assessment": structural_note,
        },
        "opportunities_recorded": len(opportunities),
    }


def write_replay_outputs(
    out_dir: Path,
    *,
    summary: dict[str, Any],
    results: list[dict[str, Any]],
    opportunities: list[Opportunity],
    temporal: dict[str, TemporalBasisCompression],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metrics.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_csv(out_dir / "dynamic_hedge_results.csv", [_flatten_result(row) for row in results])
    _write_csv(out_dir / "temporal_basis_compression.csv", [item.summary_dict() for item in temporal.values()])
    _write_csv(out_dir / "opportunities.csv", [_dataclass_dict(opp) for opp in opportunities])
    observations: list[dict[str, Any]] = []
    for item in temporal.values():
        observations.extend(item.observations)
    _write_csv(out_dir / "temporal_observations.csv", observations)
    (out_dir / "README.txt").write_text(
        "Dynamic Complement Hedge Bot replay artifacts. Paper/read-only only.\n"
        "CSV files include per-market results, temporal basis compression observations, and opportunity geometry.\n"
        "Use temporal_observations.csv to chart compression versus seconds_to_close, abs_distance_from_strike, and BTC movement/volatility proxies.\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def _flatten_result(row: dict[str, Any]) -> dict[str, Any]:
    flattened = dict(row)
    flattened["fills"] = json.dumps(row.get("fills") or [], sort_keys=True)
    return flattened


def _snapshot_from_row(row: sqlite3.Row) -> Snapshot:
    ts = _parse_dt(row["ts"])
    close = _parse_dt(row["market_close_time"])
    open_time = _parse_dt(row["market_open_time"]) if _has_column(row, "market_open_time") and row["market_open_time"] else None
    btc_price = float(row["btc_price"])
    strike = float(row["strike"])
    distance = _optional_float(row, "distance_from_strike")
    seconds = _optional_float(row, "seconds_to_close")
    return Snapshot(
        ts=ts,
        market_ticker=str(row["market_ticker"]),
        market_open_time=open_time,
        market_close_time=close,
        btc_price=btc_price,
        strike=strike,
        distance_from_strike=distance if distance is not None else btc_price - strike,
        seconds_to_close=seconds if seconds is not None else (close - ts).total_seconds(),
        yes_bid=_optional_float(row, "yes_bid"),
        yes_ask=_optional_float(row, "yes_ask"),
        no_bid=_optional_float(row, "no_bid"),
        no_ask=_optional_float(row, "no_ask"),
        slope=_optional_float(row, "slope_30s") if _has_column(row, "slope_30s") else _optional_float(row, "btc_velocity_30s"),
    )


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


def _sum_optional(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return a + b


def _dataclass_dict(item: Any) -> dict[str, Any]:
    data = dict(item.__dict__)
    for key, value in list(data.items()):
        if isinstance(value, datetime):
            data[key] = value.isoformat()
    return data
