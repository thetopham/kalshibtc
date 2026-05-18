#!/usr/bin/env python3
"""Same-window v1/v2 replay comparison for seed cheap accumulate repair.

Read-only against feed SQLite DBs. Writes one JSON artifact under runs/strategy_comparisons/.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kalshibtc.backtest.metrics import compute_metrics  # noqa: E402
from kalshibtc.execution.paper import PaperFill  # noqa: E402

DEFAULT_FROM = "2026-05-18T08:30:00+00:00"
DEFAULT_TO = "2026-05-18T15:14:58+00:00"

V1_BASELINE = {
    "strategy": "seed_cheap_accumulate_repair_v1",
    "seed_primary_spend": 90.0,
    "seed_hedge_spend": 30.0,
    "max_total_cost": 200.0,
    "max_net_ratio": 0.35,
    "repair_start_seconds": 360.0,
    "repair_max_price": 0.85,
}

V2_GRID = [
    {
        "strategy": "seed_cheap_accumulate_repair_v2",
        "seed_mode": seed_mode,
        "seed_primary_spend": primary,
        "seed_hedge_spend": hedge,
        "target_pair_cost": target_pair_cost,
        "max_total_cost": 200.0,
        "repair_max_price": 0.85,
    }
    for target_pair_cost in (0.90, 0.95, 0.98)
    for seed_mode, primary, hedge in (
        ("directional", 90.0, 30.0),
        ("balanced", 30.0, 30.0),
        ("cheap_only", 0.0, 0.0),
    )
]


@dataclass(frozen=True)
class Snapshot:
    ts: datetime
    market_ticker: str
    close_time: datetime
    strike: float
    btc_price: float
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None

    @property
    def seconds_to_close(self) -> float:
        return (self.close_time - self.ts).total_seconds()


@dataclass
class SimState:
    market_ticker: str | None = None
    yes_qty: float = 0.0
    no_qty: float = 0.0
    yes_cost: float = 0.0
    no_cost: float = 0.0
    seed_primary_side: str | None = None
    seed_primary_filled: bool = False
    seed_hedge_filled: bool = False
    active_order_keys: set[str] = field(default_factory=set)
    filled_order_keys: set[str] = field(default_factory=set)

    def reset(self, market_ticker: str) -> None:
        self.market_ticker = market_ticker
        self.yes_qty = self.no_qty = self.yes_cost = self.no_cost = 0.0
        self.seed_primary_side = None
        self.seed_primary_filled = False
        self.seed_hedge_filled = False
        self.active_order_keys.clear()
        self.filled_order_keys.clear()

    @property
    def total_cost(self) -> float:
        return self.yes_cost + self.no_cost

    @property
    def gross_qty(self) -> float:
        return self.yes_qty + self.no_qty

    @property
    def net_qty(self) -> float:
        return self.yes_qty - self.no_qty

    @property
    def net_ratio(self) -> float:
        denom = max(self.yes_qty, self.no_qty)
        return abs(self.net_qty) / denom if denom > 0 else 0.0

    @property
    def gross_net_ratio(self) -> float:
        return abs(self.net_qty) / self.gross_qty if self.gross_qty > 0 else 0.0

    def is_dominant(self, side: str) -> bool:
        return self.yes_qty > self.no_qty if side == "yes" else self.no_qty > self.yes_qty


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="from_ts", default=DEFAULT_FROM)
    parser.add_argument("--to", dest="to_ts", default=DEFAULT_TO)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    venues = {
        "polymarket": ROOT / "feed" / "polymarket-btc-1s.sqlite3",
        "kalshi": ROOT / "feed" / "kalshi-btc-1s.sqlite3",
    }
    configs = [V1_BASELINE, *V2_GRID]
    results: list[dict[str, Any]] = []
    for venue, db in venues.items():
        snapshots = load_snapshots(db, from_ts=args.from_ts, to_ts=args.to_ts)
        settlements = final_snapshot_outcomes(snapshots)
        for params in configs:
            row = run_one(params, snapshots=snapshots, settlements=settlements)
            row["venue"] = venue
            row["feed_db"] = str(db)
            row["window"] = {"from": args.from_ts, "to": args.to_ts}
            results.append(row)

    baselines = {r["venue"]: r for r in results if r["strategy"] == "seed_cheap_accumulate_repair_v1"}
    for row in results:
        base = baselines[row["venue"]]
        row["vs_v1"] = {
            "pnl_delta": round(row["realized_pnl"] - base["realized_pnl"], 6),
            "unpaired_abs_reduction_pct": reduction_pct(abs(base["unpaired_pnl"]), abs(row["unpaired_pnl"])),
            "drawdown_improvement": round(base["max_drawdown"] - row["max_drawdown"], 6),
        }
        row["success"] = bool(
            row["strategy"] == "seed_cheap_accumulate_repair_v2"
            and row["completed_pair_pnl"] > 0
            and abs(row["unpaired_pnl"]) <= 0.5 * abs(base["unpaired_pnl"])
            and row["realized_pnl"] > base["realized_pnl"]
            and row["max_drawdown"] < base["max_drawdown"]
        )

    artifact = {
        "created_at": datetime.now(tz=UTC).isoformat(),
        "window": {"from": args.from_ts, "to": args.to_ts},
        "configs": configs,
        "success_condition": [
            "completed_pair_pnl > 0",
            "abs(unpaired_pnl) drops by at least 50% vs same-venue v1",
            "realized_pnl improves vs same-venue v1",
            "max_drawdown improves vs same-venue v1",
        ],
        "results": results,
        "best_by_venue": {
            venue: sorted(
                [r for r in results if r["venue"] == venue and r["strategy"] == "seed_cheap_accumulate_repair_v2"],
                key=lambda r: (r["success"], r["realized_pnl"], -abs(r["unpaired_pnl"])),
                reverse=True,
            )[:5]
            for venue in venues
        },
    }
    out = args.out or ROOT / "runs" / "strategy_comparisons" / f"seed-v1-v2-same-window-{datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(out), "best_by_venue": artifact["best_by_venue"]}, sort_keys=True))
    return 0


def run_one(params: dict[str, Any], *, snapshots: list[Snapshot], settlements: dict[str, str]) -> dict[str, Any]:
    state = SimState()
    pending: dict[str, Any] | None = None
    fills: list[PaperFill] = []
    fills_by_market: dict[str, int] = {}
    exposure: dict[str, float] = {}
    locked = 0.0
    max_capital_used = 0.0

    for snap in snapshots:
        if state.market_ticker != snap.market_ticker:
            if state.market_ticker is not None:
                locked = max(0.0, locked - exposure.pop(state.market_ticker, 0.0))
            state.reset(snap.market_ticker)
            pending = None

        if pending is not None:
            fill = try_fill_pending(pending, snap, params["strategy"])
            if fill is not None:
                market_exposure = exposure.get(fill.market_ticker, 0.0)
                if locked + fill.notional <= 10000.0 and market_exposure + fill.notional <= params.get("max_total_cost", 200.0):
                    fills.append(fill)
                    fills_by_market[fill.market_ticker] = fills_by_market.get(fill.market_ticker, 0) + 1
                    exposure[fill.market_ticker] = market_exposure + fill.notional
                    locked += fill.notional
                    max_capital_used = max(max_capital_used, locked)
                    apply_fill(state, fill)
            pending = None

        signal = choose_signal(state, snap, params)
        if signal is not None and risk_allows(signal, snap):
            pending = signal

    agg, market_rows = settle_fills(fills, settlements)
    settled_fills = estimate_fill_pnls(fills, settlements)
    inst = dict(compute_metrics(settled_fills)) if settled_fills else {}
    pair_pnl = agg["completed_pair_pnl"]
    unpaired_pnl = agg["unpaired_pnl"]
    return {
        "strategy": params["strategy"],
        "params": params,
        "param_label": format_params(params),
        "markets_traded": len(fills_by_market),
        "settled_markets": len(market_rows),
        "fills": len(fills),
        "notional": round(sum(fill.notional for fill in fills), 6),
        "realized_pnl": round(agg["realized_pnl"], 6),
        "completed_pair_pnl": round(pair_pnl, 6),
        "unpaired_pnl": round(unpaired_pnl, 6),
        "unpaired_leakage_ratio": round(abs(unpaired_pnl) / max(pair_pnl, 1.0), 6),
        "completed_pair_contracts": round(agg["completed_pair_contracts"], 6),
        "unpaired_leftover_cost": round(agg["unpaired_leftover_cost"], 6),
        "unpaired_leftover_payout": round(agg["unpaired_leftover_payout"], 6),
        "max_drawdown": round(fnum(inst.get("max_drawdown")), 6),
        "profit_factor": clean_profit_factor(inst.get("profit_factor")),
        "win_rate": round(fnum(inst.get("win_rate")), 6),
        "max_capital_used": round(max_capital_used, 6),
        "markets": sorted(fills_by_market),
        "fills_by_market": fills_by_market,
    }


def choose_signal(state: SimState, snap: Snapshot, params: dict[str, Any]) -> dict[str, Any] | None:
    if snap.yes_ask is None or snap.no_ask is None:
        return None
    if snap.seconds_to_close <= 15.0:
        return None
    if params["strategy"] == "seed_cheap_accumulate_repair_v1":
        return choose_signal_v1(state, snap, params)
    return choose_signal_v2(state, snap, params)


def choose_signal_v1(state: SimState, snap: Snapshot, params: dict[str, Any]) -> dict[str, Any] | None:
    if snap.seconds_to_close <= params["repair_start_seconds"]:
        return repair_signal_v1(state, snap, params)
    seed = seed_signal_v1(state, snap, params)
    if seed is not None:
        return seed
    remaining = params["max_total_cost"] - state.total_cost
    if remaining <= 0:
        return None
    cheap_side = "yes" if snap.yes_ask <= snap.no_ask else "no"
    cheap_price = snap.yes_ask if cheap_side == "yes" else snap.no_ask
    if state.is_dominant(cheap_side) and state.gross_net_ratio > params["max_net_ratio"]:
        return None
    if cheap_price <= 0.08:
        return buy_signal(state, side=cheap_side, price=cheap_price, spend=min(2.0, remaining), reason="very cheap side accumulation")
    if cheap_price <= 0.15:
        return buy_signal(state, side=cheap_side, price=cheap_price, spend=min(1.0, remaining), reason="cheap side accumulation")
    return None


def choose_signal_v2(state: SimState, snap: Snapshot, params: dict[str, Any]) -> dict[str, Any] | None:
    if snap.seconds_to_close <= 120.0:
        if state.gross_qty <= 0:
            return None
        return repair_signal_v2(state, snap, params, require_cap_breach=False, reason="final neutralization repair")
    seed = seed_signal_v2(state, snap, params)
    if seed is not None:
        return seed
    repair = repair_signal_v2(state, snap, params, require_cap_breach=True, reason="continuous repair reduces exposure")
    if repair is not None:
        return repair
    remaining = params["max_total_cost"] - state.total_cost
    if remaining <= 0:
        return None
    cheap_side = "yes" if snap.yes_ask <= snap.no_ask else "no"
    cheap_price = snap.yes_ask if cheap_side == "yes" else snap.no_ask
    if state.is_dominant(cheap_side):
        return None
    if cheap_price <= 0.08:
        return buy_signal(state, side=cheap_side, price=cheap_price, spend=min(2.0, remaining), reason="very cheap exposure-reducing accumulation")
    if cheap_price <= 0.15:
        return buy_signal(state, side=cheap_side, price=cheap_price, spend=min(1.0, remaining), reason="cheap exposure-reducing accumulation")
    return None


def seed_signal_v1(state: SimState, snap: Snapshot, params: dict[str, Any]) -> dict[str, Any] | None:
    if not (720.0 <= snap.seconds_to_close <= 900.0):
        return None
    if state.seed_primary_side is None:
        state.seed_primary_side = primary_side(snap)
    remaining = params["max_total_cost"] - state.total_cost
    if remaining <= 0:
        return None
    if not state.seed_primary_filled:
        side = state.seed_primary_side
        price = snap.yes_ask if side == "yes" else snap.no_ask
        return buy_signal(state, side=side, price=price, spend=min(params["seed_primary_spend"], remaining), reason="opening seed primary 3x")
    if not state.seed_hedge_filled:
        side = opposite(state.seed_primary_side)
        price = snap.yes_ask if side == "yes" else snap.no_ask
        return buy_signal(state, side=side, price=price, spend=min(params["seed_hedge_spend"], remaining), reason="opening seed hedge 1x")
    return None


def seed_signal_v2(state: SimState, snap: Snapshot, params: dict[str, Any]) -> dict[str, Any] | None:
    if not (720.0 <= snap.seconds_to_close <= 900.0):
        return None
    remaining = params["max_total_cost"] - state.total_cost
    if remaining <= 0:
        return None
    seed_mode = params["seed_mode"]
    if seed_mode == "cheap_only":
        if state.seed_primary_filled:
            return None
        cheap_side = "yes" if snap.yes_ask <= snap.no_ask else "no"
        cheap_price = snap.yes_ask if cheap_side == "yes" else snap.no_ask
        if cheap_price > 0.15:
            return None
        state.seed_primary_side = cheap_side
        return buy_signal(state, side=cheap_side, price=cheap_price, spend=min(1.0, remaining), reason="cheap-only seed")
    if state.seed_primary_side is None:
        state.seed_primary_side = primary_side(snap)
    if not state.seed_primary_filled:
        side = state.seed_primary_side
        price = snap.yes_ask if side == "yes" else snap.no_ask
        return buy_signal(state, side=side, price=price, spend=min(params["seed_primary_spend"], remaining), reason=f"opening {seed_mode} primary")
    if not state.seed_hedge_filled:
        side = opposite(state.seed_primary_side)
        price = snap.yes_ask if side == "yes" else snap.no_ask
        return buy_signal(state, side=side, price=price, spend=min(params["seed_hedge_spend"], remaining), reason=f"opening {seed_mode} hedge")
    return None


def repair_signal_v1(state: SimState, snap: Snapshot, params: dict[str, Any]) -> dict[str, Any] | None:
    if state.gross_qty <= 0 or state.net_ratio <= 0.05:
        return None
    side, price, contracts_needed = repair_side_price_qty(state, snap)
    if price is None or price > params["repair_max_price"]:
        return None
    remaining = params["max_total_cost"] - state.total_cost
    if remaining <= 0:
        return None
    return buy_signal(state, side=side, price=price, spend=min(contracts_needed * price, remaining), reason="repair smaller side near expiry")


def repair_signal_v2(state: SimState, snap: Snapshot, params: dict[str, Any], *, require_cap_breach: bool, reason: str) -> dict[str, Any] | None:
    if state.gross_qty <= 0 or state.net_ratio <= 0.05:
        return None
    if require_cap_breach and state.net_ratio <= dynamic_net_cap(snap.seconds_to_close):
        return None
    side, price, contracts_needed = repair_side_price_qty(state, snap)
    if price is None or price > params["repair_max_price"]:
        return None
    if not pair_cost_allows(state, side=side, price=price, qty=contracts_needed, target=params["target_pair_cost"]):
        return None
    remaining = params["max_total_cost"] - state.total_cost
    if remaining <= 0:
        return None
    return buy_signal(state, side=side, price=price, spend=min(contracts_needed * price, remaining), reason=reason)


def buy_signal(state: SimState, *, side: str, price: float | None, spend: float, reason: str) -> dict[str, Any] | None:
    if price is None or price <= 0 or spend <= 0:
        return None
    contracts = spend / price
    if contracts < 5.0:
        return None
    key = f"{side}:{reason}:{round(price, 4):.4f}"
    if key in state.active_order_keys or key in state.filled_order_keys:
        return None
    state.active_order_keys.add(key)
    return {"side": side, "limit_price": price, "spend": round(spend, 6), "key": key}


def risk_allows(signal: dict[str, Any], snap: Snapshot) -> bool:
    prices = (snap.yes_bid, snap.yes_ask, snap.no_bid, snap.no_ask)
    if not all(p is not None and 0.0 < p < 1.0 for p in prices):
        return False
    assert snap.yes_bid is not None and snap.yes_ask is not None and snap.no_bid is not None and snap.no_ask is not None
    return snap.yes_bid <= snap.yes_ask and snap.no_bid <= snap.no_ask


def try_fill_pending(signal: dict[str, Any], snap: Snapshot, strategy: str) -> PaperFill | None:
    if not risk_allows(signal, snap):
        return None
    side = signal["side"]
    entry = snap.yes_ask if side == "yes" else snap.no_ask
    if entry is None or entry > signal["limit_price"]:
        return None
    return PaperFill(
        strategy=strategy,
        market_ticker=snap.market_ticker,
        side="long_above" if side == "yes" else "long_below",
        entry_price=entry,
        notional=signal["spend"],
        contracts=signal["spend"] / entry,
        ts=snap.ts,
    )


def apply_fill(state: SimState, fill: PaperFill) -> None:
    side = "yes" if fill.side == "long_above" else "no"
    if side == "yes":
        state.yes_qty += fill.contracts
        state.yes_cost += fill.notional
    else:
        state.no_qty += fill.contracts
        state.no_cost += fill.notional
    if state.seed_primary_side is not None and not state.seed_primary_filled and side == state.seed_primary_side:
        state.seed_primary_filled = True
    elif state.seed_primary_side is not None and state.seed_primary_filled and not state.seed_hedge_filled and side != state.seed_primary_side:
        state.seed_hedge_filled = True
    prefix = f"{side}:"
    suffix = f":{round(fill.entry_price, 4):.4f}"
    matched = {key for key in state.active_order_keys if key.startswith(prefix) and key.endswith(suffix)}
    state.active_order_keys.difference_update(matched)
    state.filled_order_keys.update(matched)


def settle_fills(fills: list[PaperFill], settlements: dict[str, str]) -> tuple[dict[str, float], list[dict[str, float]]]:
    by_market: dict[str, list[PaperFill]] = {}
    for fill in fills:
        by_market.setdefault(fill.market_ticker, []).append(fill)
    markets = []
    for market, market_fills in by_market.items():
        outcome = settlements.get(market)
        yes_cost = sum(f.notional for f in market_fills if f.side == "long_above")
        no_cost = sum(f.notional for f in market_fills if f.side == "long_below")
        payout = sum(f.contracts for f in market_fills if (f.side == "long_above" and outcome == "above") or (f.side == "long_below" and outcome == "below"))
        split = pair_split(market_fills, outcome)
        markets.append({"realized_pnl": payout - yes_cost - no_cost, **split})
    agg = {
        "realized_pnl": sum(m["realized_pnl"] for m in markets),
        "completed_pair_pnl": sum(m["completed_pair_pnl"] for m in markets),
        "completed_pair_contracts": sum(m["completed_pair_contracts"] for m in markets),
        "unpaired_pnl": sum(m["unpaired_leftover_pnl"] for m in markets),
        "unpaired_leftover_cost": sum(m["unpaired_leftover_cost"] for m in markets),
        "unpaired_leftover_payout": sum(m["unpaired_leftover_payout"] for m in markets),
    }
    return agg, markets


def pair_split(fills: list[PaperFill], outcome: str | None) -> dict[str, float]:
    yes_lots = [[f.contracts, f.entry_price] for f in fills if f.side == "long_above"]
    no_lots = [[f.contracts, f.entry_price] for f in fills if f.side == "long_below"]
    pair_contracts = pair_cost = pair_payout = 0.0
    yi = ni = 0
    while yi < len(yes_lots) and ni < len(no_lots):
        qty = min(yes_lots[yi][0], no_lots[ni][0])
        if qty <= 1e-9:
            break
        pair_contracts += qty
        pair_cost += qty * yes_lots[yi][1] + qty * no_lots[ni][1]
        pair_payout += qty if outcome in {"above", "below"} else 0.0
        yes_lots[yi][0] -= qty
        no_lots[ni][0] -= qty
        if yes_lots[yi][0] <= 1e-9:
            yi += 1
        if no_lots[ni][0] <= 1e-9:
            ni += 1
    rem_yes = yes_lots[yi:]
    rem_no = no_lots[ni:]
    unpaired_cost = sum(q * p for q, p in rem_yes) + sum(q * p for q, p in rem_no)
    unpaired_payout = (sum(q for q, _ in rem_yes) if outcome == "above" else 0.0) + (sum(q for q, _ in rem_no) if outcome == "below" else 0.0)
    return {
        "completed_pair_contracts": pair_contracts,
        "completed_pair_pnl": pair_payout - pair_cost,
        "unpaired_leftover_cost": unpaired_cost,
        "unpaired_leftover_payout": unpaired_payout,
        "unpaired_leftover_pnl": unpaired_payout - unpaired_cost,
    }


def estimate_fill_pnls(fills: list[PaperFill], settlements: dict[str, str]) -> list[dict[str, Any]]:
    rows = []
    for fill in fills:
        outcome = settlements.get(fill.market_ticker)
        won = (fill.side == "long_above" and outcome == "above") or (fill.side == "long_below" and outcome == "below")
        exit_price = 1.0 if won else 0.0
        rows.append({"strategy": fill.strategy, "market_ticker": fill.market_ticker, "side": fill.side, "entry_price": fill.entry_price, "notional": fill.notional, "contracts": fill.contracts, "ts": fill.ts, "pnl": fill.contracts * exit_price - fill.notional, "settlement_result": outcome, "exit_price": exit_price})
    return rows


def load_snapshots(feed_db: Path, *, from_ts: str, to_ts: str) -> list[Snapshot]:
    sql = "SELECT * FROM realtime_snapshots_1s WHERE ts >= ? AND ts <= ? ORDER BY ts ASC, market_ticker ASC"
    with sqlite3.connect(f"file:{feed_db}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = list(conn.execute(sql, [from_ts, to_ts]))
    snapshots = []
    for row in rows:
        strike = first_float(row, "strike", "target_price")
        if strike <= 0:
            continue
        snapshots.append(Snapshot(ts=parse_dt(row["ts"]), market_ticker=str(row["market_ticker"]), close_time=parse_dt(row["market_close_time"]), strike=strike, btc_price=as_float(row["btc_price"]), yes_bid=optional_float(row, "yes_bid"), yes_ask=optional_float(row, "yes_ask"), no_bid=optional_float(row, "no_bid"), no_ask=optional_float(row, "no_ask")))
    return snapshots


def final_snapshot_outcomes(snapshots: list[Snapshot]) -> dict[str, str]:
    last: dict[str, Snapshot] = {}
    for snap in snapshots:
        last[snap.market_ticker] = snap
    return {market: "above" if snap.btc_price > snap.strike else "below" if snap.btc_price < snap.strike else "at" for market, snap in last.items()}


def primary_side(snap: Snapshot) -> str:
    if snap.btc_price > snap.strike:
        return "yes"
    if snap.btc_price < snap.strike:
        return "no"
    return "yes"


def opposite(side: str) -> str:
    return "no" if side == "yes" else "yes"


def repair_side_price_qty(state: SimState, snap: Snapshot) -> tuple[str, float | None, float]:
    if state.yes_qty > state.no_qty:
        return "no", snap.no_ask, state.yes_qty - state.no_qty
    return "yes", snap.yes_ask, state.no_qty - state.yes_qty


def pair_cost_allows(state: SimState, *, side: str, price: float, qty: float, target: float) -> bool:
    yes_qty = state.yes_qty + (qty if side == "yes" else 0.0)
    no_qty = state.no_qty + (qty if side == "no" else 0.0)
    yes_cost = state.yes_cost + (qty * price if side == "yes" else 0.0)
    no_cost = state.no_cost + (qty * price if side == "no" else 0.0)
    if yes_qty <= 0 or no_qty <= 0:
        return True
    return (yes_cost / yes_qty) + (no_cost / no_qty) <= target


def dynamic_net_cap(seconds_to_close: float) -> float:
    if seconds_to_close < 120:
        return 0.05
    if seconds_to_close < 360:
        return 0.15
    if seconds_to_close < 720:
        return 0.20
    return 0.25


def parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def optional_float(row: sqlite3.Row, name: str) -> float | None:
    try:
        value = row[name]
    except (KeyError, IndexError):
        return None
    if value is None:
        return None
    return float(value)


def first_float(row: sqlite3.Row, *names: str) -> float:
    for name in names:
        value = optional_float(row, name)
        if value is not None:
            return value
    return 0.0


def as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def fnum(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def clean_profit_factor(value: Any) -> float | str:
    val = fnum(value)
    if math.isfinite(val):
        return round(val, 6)
    return str(value)


def reduction_pct(old_abs: float, new_abs: float) -> float:
    if old_abs <= 0:
        return 0.0
    return round((old_abs - new_abs) / old_abs * 100.0, 6)


def format_params(params: dict[str, Any]) -> str:
    keys = ["strategy", "seed_mode", "seed_primary_spend", "seed_hedge_spend", "target_pair_cost", "repair_max_price", "max_total_cost"]
    return ",".join(f"{k}={params[k]}" for k in keys if k in params)


if __name__ == "__main__":
    raise SystemExit(main())
