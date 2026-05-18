#!/usr/bin/env python3
"""Liquidity-aware v2 parameter sweep on same-window BTC 15m replay data."""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kalshibtc.backtest.metrics import compute_metrics  # noqa: E402
from kalshibtc.config import BotConfig, RiskLimits  # noqa: E402
from kalshibtc.datafeed.models import OrderBookSnapshot, Tick  # noqa: E402
from kalshibtc.execution.paper import PaperExecutor, PaperFill  # noqa: E402
from kalshibtc.main import MarketStateBuilder  # noqa: E402
from kalshibtc.market.contract import ContractWindow  # noqa: E402
from kalshibtc.strategy.seed_cheap_accumulate_repair_v2 import SeedCheapAccumulateRepairV2Config, SeedCheapAccumulateRepairV2Strategy  # noqa: E402

DEFAULT_FROM = "2026-05-18T08:30:00+00:00"
DEFAULT_TO = "2026-05-18T15:14:58+00:00"
GRID = {
    "liquidity_participation_rate": [0.10, 0.20, 0.30, 0.50],
    "min_depth_contracts": [10, 25, 50, 100],
    "max_order_notional": [5, 10, 20, 30],
    "repair_size_multiplier": [1.0, 1.5, 2.0],
    "spread_penalty_enabled": [False, True],
}
SMOKE_GRID = {
    "liquidity_participation_rate": [0.10, 0.20, 0.30, 0.50],
    "min_depth_contracts": [10, 50],
    "max_order_notional": [5, 10, 20, 30],
    "repair_size_multiplier": [1.0, 2.0],
    "spread_penalty_enabled": [False, True],
}
BASE_CONFIGS = {
    "poly_cheap_090": {"venue": "polymarket", "seed_mode": "cheap_only", "target_pair_cost": 0.90, "normal_spend": 30, "very_cheap_spend": 30},
    "kalshi_cheap_090": {"venue": "kalshi", "seed_mode": "cheap_only", "target_pair_cost": 0.90, "normal_spend": 30, "very_cheap_spend": 30},
    "kalshi_balanced_095": {"venue": "kalshi", "seed_mode": "balanced", "seed_primary_spend": 30, "seed_hedge_spend": 30, "target_pair_cost": 0.95, "normal_spend": 30, "very_cheap_spend": 30},
}
DBS = {"polymarket": ROOT / "feed" / "polymarket-btc-1s.sqlite3", "kalshi": ROOT / "feed" / "kalshi-btc-1s.sqlite3"}

@dataclass(frozen=True)
class Snapshot:
    ts: datetime; market_ticker: str; open_time: datetime; close_time: datetime; strike: float; btc_price: float
    yes_bid: float|None; yes_ask: float|None; no_bid: float|None; no_ask: float|None; raw: dict[str, Any]
    @property
    def seconds_to_close(self)->float: return (self.close_time-self.ts).total_seconds()


def main() -> int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--from', dest='from_ts', default=DEFAULT_FROM); ap.add_argument('--to', dest='to_ts', default=DEFAULT_TO)
    ap.add_argument('--out', type=Path, default=None)
    ap.add_argument('--smoke-grid', action='store_true', help='Run a smaller representative grid for fast iteration.')
    args=ap.parse_args()
    snapshots_by_venue={v: load_snapshots(db,args.from_ts,args.to_ts) for v,db in DBS.items()}
    settlements_by_venue={v: final_snapshot_outcomes(snaps) for v,snaps in snapshots_by_venue.items()}
    results=[]
    grid = SMOKE_GRID if args.smoke_grid else GRID
    for base_name, base in BASE_CONFIGS.items():
        for grid_params in grid_configs(grid):
            params={**base, **grid_params, "max_total_cost":200.0, "repair_max_price":0.85, "cheap_persistence_seconds_for_boost":60.0, "cheap_persistence_size_multiplier":1.25}
            row=run_one(params, snapshots_by_venue[base['venue']], settlements_by_venue[base['venue']])
            row['base_name']=base_name; row['params']=params; row['venue']=base['venue']; results.append(row)
    for r in results:
        r['accepted']=r['completed_pair_pnl']>0 and r['realized_pnl']>0
    artifact={
        'created_at': datetime.now(tz=UTC).isoformat(), 'window': {'from':args.from_ts,'to':args.to_ts}, 'grid':grid,
        'results': results,
        'best_by_base': {name: sorted([r for r in results if r['base_name']==name], key=lambda r:(r['accepted'], r['realized_pnl'], r['pnl_per_notional'], -r['max_drawdown']), reverse=True)[:10] for name in BASE_CONFIGS},
    }
    out=args.out or ROOT/'runs'/'strategy_comparisons'/f"seed-v2-liquidity-sweep-{datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.parent.mkdir(parents=True, exist_ok=True); out.write_text(json.dumps(artifact, indent=2, sort_keys=True)+'\n')
    print(json.dumps({'out':str(out),'best_by_base':artifact['best_by_base']}, sort_keys=True))
    return 0


def run_one(params:dict[str,Any], snapshots:list[Snapshot], settlements:dict[str,str])->dict[str,Any]:
    strategy=SeedCheapAccumulateRepairV2Strategy(SeedCheapAccumulateRepairV2Config(**{k:v for k,v in params.items() if k!='venue'}))
    pending=None; fills=[]; fills_by_market={}; exposure={}; locked=max_cap=0.0
    active_contract=None; builder=None
    for snap in snapshots:
        if active_contract is None or active_contract.ticker != snap.market_ticker:
            if active_contract is not None: locked=max(0, locked-exposure.pop(active_contract.ticker,0))
            active_contract=ContractWindow(snap.market_ticker, snap.strike, snap.close_time, snap.open_time); builder=MarketStateBuilder(contract=active_contract); pending=None
        assert builder is not None
        state=builder.from_tick_and_book(tick=Tick(ts=snap.ts, price=snap.btc_price, source='replay'), orderbook=OrderBookSnapshot(ts=snap.ts, market_ticker=snap.market_ticker, yes_bid=snap.yes_bid, yes_ask=snap.yes_ask, no_bid=snap.no_bid, no_ask=snap.no_ask, raw=snap.raw), slope_30s=None)
        if pending is not None:
            fill=try_fill_pending(pending, state)
            if fill is not None:
                me=exposure.get(fill.market_ticker,0.0)
                if locked+fill.notional<=10000 and me+fill.notional<=200:
                    fills.append(fill); fills_by_market[fill.market_ticker]=fills_by_market.get(fill.market_ticker,0)+1; exposure[fill.market_ticker]=me+fill.notional; locked+=fill.notional; max_cap=max(max_cap,locked); strategy.on_fill(state,fill)
            pending=None
        sig=strategy.on_tick(state)
        if sig.side!='none': pending=sig
    agg, market_rows, worst=settle_fills(fills, settlements)
    settled=estimate_fill_pnls(fills, settlements); inst=dict(compute_metrics(settled)) if settled else {}
    notional=sum(f.notional for f in fills); pair=agg['completed_pair_pnl']; unp=agg['unpaired_pnl']
    return {'realized_pnl':round(agg['realized_pnl'],6),'pnl_per_notional':round(agg['realized_pnl']/notional,6) if notional else 0.0,'profit_factor':clean_profit_factor(inst.get('profit_factor')),'max_drawdown':round(fnum(inst.get('max_drawdown')),6),'completed_pair_pnl':round(pair,6),'unpaired_pnl':round(unp,6),'unpaired_leakage_ratio':round(abs(unp)/max(pair,1.0),6),'fills':len(fills),'avg_size':round(sum(f.contracts for f in fills)/len(fills),6) if fills else 0.0,'notional':round(notional,6),'markets_traded':len(fills_by_market),'max_capital_used':round(max_cap,6),'worst_5_markets':worst[:5]}

def try_fill_pending(signal, state):
    side = signal.side
    entry = state.orderbook.yes_ask if side == 'long_above' else state.orderbook.no_ask if side == 'long_below' else None
    limit = (signal.features or {}).get('limit_price')
    if entry is None or limit is None or entry > limit:
        return None
    notional = float(signal.target_notional or 0.0)
    if notional <= 0:
        return None
    return PaperFill(strategy=signal.strategy, market_ticker=state.contract.ticker, side=side, entry_price=entry, notional=notional, contracts=notional/entry, ts=state.tick.ts)

# Settlement / loading helpers copied from comparison script shape
def settle_fills(fills, settlements):
    by={}
    for f in fills: by.setdefault(f.market_ticker,[]).append(f)
    rows=[]
    for m,fs in by.items():
        outcome=settlements.get(m); payout=sum(f.contracts for f in fs if (f.side=='long_above' and outcome=='above') or (f.side=='long_below' and outcome=='below')); cost=sum(f.notional for f in fs); split=pair_split(fs,outcome); rows.append({'market':m,'realized_pnl':payout-cost,**split})
    agg={'realized_pnl':sum(r['realized_pnl'] for r in rows),'completed_pair_pnl':sum(r['completed_pair_pnl'] for r in rows),'unpaired_pnl':sum(r['unpaired_leftover_pnl'] for r in rows)}
    return agg, rows, sorted(rows, key=lambda r:r['realized_pnl'])

def pair_split(fills,outcome):
    y=[[f.contracts,f.entry_price] for f in fills if f.side=='long_above']; n=[[f.contracts,f.entry_price] for f in fills if f.side=='long_below']; yi=ni=0; pc=cost=pay=0.0
    while yi<len(y) and ni<len(n):
        q=min(y[yi][0],n[ni][0]); pc+=q; cost+=q*y[yi][1]+q*n[ni][1]; pay+=q if outcome in {'above','below'} else 0; y[yi][0]-=q; n[ni][0]-=q
        if y[yi][0]<=1e-9: yi+=1
        if n[ni][0]<=1e-9: ni+=1
    remy=y[yi:]; remn=n[ni:]; uc=sum(q*p for q,p in remy)+sum(q*p for q,p in remn); up=(sum(q for q,_ in remy) if outcome=='above' else 0)+(sum(q for q,_ in remn) if outcome=='below' else 0)
    return {'completed_pair_pnl':pay-cost,'unpaired_leftover_pnl':up-uc}

def estimate_fill_pnls(fills, settlements):
    return [{'strategy':f.strategy,'market_ticker':f.market_ticker,'side':f.side,'entry_price':f.entry_price,'notional':f.notional,'contracts':f.contracts,'ts':f.ts,'pnl':f.contracts*(1.0 if ((f.side=='long_above' and settlements.get(f.market_ticker)=='above') or (f.side=='long_below' and settlements.get(f.market_ticker)=='below')) else 0.0)-f.notional} for f in fills]

def load_snapshots(db, from_ts, to_ts):
    con=sqlite3.connect(f'file:{db}?mode=ro', uri=True); con.row_factory=sqlite3.Row; rows=list(con.execute('select * from realtime_snapshots_1s where ts>=? and ts<=? order by market_ticker asc, ts asc',[from_ts,to_ts])); out=[]
    for r in rows:
        strike=first_float(r,'strike','target_price')
        if strike<=0: continue
        raw=extract_raw(r)
        out.append(Snapshot(parse_dt(r['ts']),str(r['market_ticker']),parse_dt(r['market_open_time']),parse_dt(r['market_close_time']),strike,as_float(r['btc_price']),opt(r,'yes_bid'),opt(r,'yes_ask'),opt(r,'no_bid'),opt(r,'no_ask'),raw))
    return out

def extract_raw(r):
    raw={}
    for side in ['yes','no']:
        col=f'{side}_orderbook_json'
        if col in r.keys() and r[col]:
            try: raw[f'{side}_orderbook']=json.loads(r[col])
            except Exception: pass
    if 'raw_json' in r.keys() and r['raw_json']:
        try:
            d=json.loads(r['raw_json']); ob=d.get('orderbook_raw',{}).get('orderbook_fp',{})
            if 'yes_dollars' in ob: raw['yes_orderbook']={'asks':ob['yes_dollars']}
            if 'no_dollars' in ob: raw['no_orderbook']={'asks':ob['no_dollars']}
        except Exception: pass
    return raw

def final_snapshot_outcomes(snaps):
    last={}
    for s in snaps: last[s.market_ticker]=s
    return {m:'above' if s.btc_price>s.strike else 'below' if s.btc_price<s.strike else 'at' for m,s in last.items()}
def grid_configs(grid: dict[str, list[Any]] = GRID):
    ks=list(grid); return [dict(zip(ks,vs,strict=True)) for vs in itertools.product(*(grid[k] for k in ks))]
def parse_dt(v): return datetime.fromisoformat(str(v).replace('Z','+00:00')).astimezone(UTC)
def opt(r,n):
    try: return None if r[n] is None else float(r[n])
    except Exception: return None
def first_float(r,*ns):
    for n in ns:
        v=opt(r,n)
        if v is not None: return v
    return 0.0
def as_float(v):
    try: return float(v)
    except Exception: return 0.0
def fnum(v):
    try: return 0.0 if v is None else float(v)
    except Exception: return 0.0
def clean_profit_factor(v):
    val=fnum(v); return round(val,6) if math.isfinite(val) else str(v)
if __name__=='__main__': raise SystemExit(main())
