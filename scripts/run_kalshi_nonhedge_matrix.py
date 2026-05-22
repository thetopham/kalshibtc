from __future__ import annotations

import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path('/home/matt/workspace/kalshi-btc-15m-bot')
FEED_DB = Path('/tmp/kalshi-official-nonhedge-through-20260522.sqlite3')
RUNS_DIR = REPO / 'runs'
MATRIX_DIR = REPO / 'research' / 'kalshi_nonhedge_matrix'
STAMP = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
OUT_JSON = MATRIX_DIR / f'matrix_{STAMP}.json'
OUT_CSV = MATRIX_DIR / f'matrix_{STAMP}.csv'
OUT_MD = MATRIX_DIR / f'matrix_{STAMP}.md'

INCLUDED = [
    'no_trade_baseline',
    'simple_directional',
    'mean_reversion_to_strike',
    'breakout_momentum',
    'late_window_only',
    'spread_aware_momentum',
    'strategy_probability_mm_v0',
    'bayesian_markov_directional',
]

SKIPPED = {
    'simple_inventory_mm': 'holds/builds YES+NO inventory in one market',
    'pair_arb': 'pair/inventory seed strategy; pair semantics not Kalshi single-position safe',
    'pair_arb_grid': 'pair/grid semantics can build simultaneous YES+NO',
    'pair_arb_passive': 'pair/passive semantics can build simultaneous YES+NO',
    'inventory_vol_rebalance': 'dedicated inventory manager / no generic directional signal',
    'inventory_vol_regime': 'paper-only inventory manager marker',
    'volatility_inventory': 'tracks both above/below inventory; can hold both sides',
    'hedge_volatility_v0': 'registry marks polymarket-only hedging',
    'volatility_hedge': 'registry marks polymarket-only hedging',
    'contrarian_spread_reversion': 'registry marks polymarket-only hedging',
    'complement_ladder_v0': 'registry marks polymarket-only complement/pair strategy',
    'cheap_accumulate_repair_v0': 'registry marks polymarket-only repair/hedging',
    'seed_cheap_accumulate_repair_v1': 'registry marks polymarket-only repair/hedging',
    'seed_cheap_accumulate_repair_v2': 'registry marks polymarket-only repair/hedging',
    'inventory_aware_passive_mm': 'registry marks polymarket-only inventory/pair strategy',
}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def row_from_run(strategy: str, run_id: str, run_dir: Path, proc: subprocess.CompletedProcess[str]) -> dict:
    row = {
        'strategy': strategy,
        'run_id': run_id,
        'run_dir': str(run_dir),
        'returncode': proc.returncode,
        'stdout_tail': proc.stdout[-2000:],
        'stderr_tail': proc.stderr[-2000:],
    }
    if proc.returncode != 0:
        row.update({'status': 'error'})
        return row
    metrics = load_json(run_dir / 'metrics.json')
    settlement = load_json(run_dir / 'portfolio_settlement.json')
    inst = metrics.get('institutional_metrics') or {}
    row.update({
        'status': 'ok',
        'snapshots': metrics.get('snapshots'),
        'signals': metrics.get('signals'),
        'fills': metrics.get('fills'),
        'markets': settlement.get('markets'),
        'settled_markets': settlement.get('settled_markets'),
        'metric_markets': settlement.get('metric_markets'),
        'total_cost': settlement.get('total_cost'),
        'gross_payout': settlement.get('gross_payout'),
        'realized_pnl': settlement.get('realized_pnl'),
        'paired_locked_edge': settlement.get('paired_locked_edge'),
        'final_unpaired_yes_contracts': settlement.get('final_unpaired_yes_contracts'),
        'final_unpaired_no_contracts': settlement.get('final_unpaired_no_contracts'),
        'max_abs_raw_net_contracts': settlement.get('max_abs_raw_net_contracts'),
        'win_rate': inst.get('win_rate', metrics.get('win_rate')),
        'profit_factor': inst.get('profit_factor', metrics.get('profit_factor')),
        'max_drawdown': inst.get('max_drawdown', metrics.get('max_drawdown')),
        'largest_win': inst.get('largest_win', metrics.get('largest_win')),
        'largest_loss': inst.get('largest_loss', metrics.get('largest_loss')),
        'sharpe_ratio': inst.get('sharpe_ratio', metrics.get('sharpe_ratio')),
    })
    return row


def main() -> int:
    MATRIX_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for strategy in INCLUDED:
        run_id = f'kalshi-nonhedge-through-20260522-{STAMP}-{strategy}'
        cmd = [
            sys.executable, '-m', 'kalshibtc.replay.cli',
            '--feed-db', str(FEED_DB),
            '--runs-dir', str(RUNS_DIR),
            '--strategy', strategy,
            '--venue', 'kalshi',
            '--run-id', run_id,
            '--base-size-dollars', '200',
            '--max-position-dollars', '200',
            '--max-open-positions', '100000',
            '--max-spread', '1.0',
            '--starting-bankroll', '10000',
            '--max-capital-at-risk', '10000',
            '--per-market-max-exposure', '200',
            '--settlements-from-feed-db',
            '--fill-timing', 'next-tick',
            '--json',
            '--overwrite',
        ]
        print('RUN', strategy, flush=True)
        proc = subprocess.run(cmd, cwd=REPO, text=True, capture_output=True, timeout=900)
        run_dir = RUNS_DIR / strategy / run_id
        row = row_from_run(strategy, run_id, run_dir, proc)
        rows.append(row)
        OUT_JSON.write_text(json.dumps({'generated_at': STAMP, 'feed_db': str(FEED_DB), 'included': INCLUDED, 'skipped': SKIPPED, 'rows': rows}, indent=2, sort_keys=True) + '\n')
    fieldnames = sorted({k for row in rows for k in row})
    with OUT_CSV.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader(); w.writerows(rows)
    sorted_rows = sorted(rows, key=lambda r: float(r.get('realized_pnl') or -10**12), reverse=True)
    lines = [f'# Kalshi non-hedging matrix {STAMP}', '', f'Feed DB: `{FEED_DB}`', '', '## Included', '']
    lines += [f'- `{s}`' for s in INCLUDED]
    lines += ['', '## Skipped/ineligible', '']
    lines += [f'- `{k}`: {v}' for k, v in sorted(SKIPPED.items())]
    lines += ['', '## Leaderboard', '']
    for r in sorted_rows:
        lines.append(f"- `{r['strategy']}`: status={r.get('status')} pnl={r.get('realized_pnl')} fills={r.get('fills')} cost={r.get('total_cost')} payout={r.get('gross_payout')} worst={r.get('largest_loss')} best={r.get('largest_win')} run=`{r.get('run_dir')}`")
    OUT_MD.write_text('\n'.join(lines) + '\n')
    print(json.dumps({'json': str(OUT_JSON), 'csv': str(OUT_CSV), 'md': str(OUT_MD), 'rows': rows}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
