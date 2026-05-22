from __future__ import annotations

import csv
import itertools
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path('/home/matt/workspace/kalshi-btc-15m-bot')
FEED_DB = Path('/tmp/kalshi-official-nonhedge-through-20260522.sqlite3')
RUNS_DIR = REPO / 'runs'
OUT_DIR = REPO / 'research' / 'late_lotto_ticket_sweeps'
STAMP = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')

WINDOWS = [(30, 60), (45, 90), (60, 120), (90, 180), (120, 240)]
MAX_PRICES = [0.003, 0.005, 0.01, 0.015, 0.02, 0.03, 0.05]
MIN_PRICES = [0.001]
SIDE_MODES = ['cheapest', 'no_only', 'yes_only']
BASE_NOTIONALS = [5.0, 10.0]

OUT_JSON = OUT_DIR / f'sweep_{STAMP}.json'
OUT_CSV = OUT_DIR / f'sweep_{STAMP}.csv'
OUT_MD = OUT_DIR / f'sweep_{STAMP}.md'
CHECKPOINT_JSON = OUT_DIR / f'sweep_{STAMP}.checkpoint.json'
CHECKPOINT_CSV = OUT_DIR / f'sweep_{STAMP}.checkpoint.csv'


def jload(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def write_checkpoint(rows: list[dict]) -> None:
    payload = {
        'generated_at': STAMP,
        'feed_db': str(FEED_DB),
        'completed': len(rows),
        'total': len(WINDOWS) * len(MAX_PRICES) * len(MIN_PRICES) * len(SIDE_MODES) * len(BASE_NOTIONALS),
        'rows': rows,
    }
    CHECKPOINT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')
    if rows:
        fields = sorted({k for r in rows for k in r})
        tmp = CHECKPOINT_CSV.with_suffix('.tmp')
        with tmp.open('w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader(); w.writerows(rows)
        tmp.replace(CHECKPOINT_CSV)


def summarize_run(row: dict, run_dir: Path) -> dict:
    metrics = jload(run_dir / 'metrics.json')
    settlement = jload(run_dir / 'portfolio_settlement.json')
    inst = metrics.get('institutional_metrics') or {}
    row.update({
        'status': 'ok',
        'snapshots': metrics.get('snapshots'),
        'signals': metrics.get('signals'),
        'fills': metrics.get('fills'),
        'markets': settlement.get('markets'),
        'total_cost': settlement.get('total_cost'),
        'gross_payout': settlement.get('gross_payout'),
        'realized_pnl': settlement.get('realized_pnl'),
        'max_abs_raw_net_contracts': settlement.get('max_abs_raw_net_contracts'),
        'win_rate': inst.get('win_rate'),
        'profit_factor': inst.get('profit_factor'),
        'max_drawdown': inst.get('max_drawdown'),
        'largest_win': inst.get('largest_win'),
        'largest_loss': inst.get('largest_loss'),
        'sharpe': inst.get('sharpe'),
        'skewness': inst.get('skewness'),
        'kurtosis': inst.get('kurtosis'),
        'run_dir': str(run_dir),
    })
    if row.get('realized_pnl') is not None and row.get('largest_win') is not None:
        pnl = float(row['realized_pnl'])
        lw = float(row['largest_win'] or 0.0)
        row['largest_win_to_pnl_ratio'] = (lw / pnl) if pnl > 0 else None
    return row


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    combos = list(itertools.product(WINDOWS, MAX_PRICES, MIN_PRICES, SIDE_MODES, BASE_NOTIONALS))
    for i, (window, max_price, min_price, side_mode, base_notional) in enumerate(combos, start=1):
        min_sec, max_sec = window
        run_id = (
            f'late-lotto-sweep-{STAMP}-'
            f'w{int(min_sec)}-{int(max_sec)}-p{str(max_price).replace(".", "p")}-'
            f'min{str(min_price).replace(".", "p")}-{side_mode}-n{str(base_notional).replace(".", "p")}'
        )
        run_dir = RUNS_DIR / 'late_lotto_ticket' / run_id
        row = {
            'idx': i,
            'run_id': run_id,
            'min_seconds_to_close': min_sec,
            'max_seconds_to_close': max_sec,
            'max_ticket_price': max_price,
            'min_ticket_price': min_price,
            'side_mode': side_mode,
            'base_notional': base_notional,
        }
        cmd = [
            sys.executable, '-m', 'kalshibtc.replay.cli',
            '--feed-db', str(FEED_DB),
            '--runs-dir', str(RUNS_DIR),
            '--strategy', 'late_lotto_ticket',
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
            '--strategy-param', f'min_seconds_to_close={min_sec}',
            '--strategy-param', f'max_seconds_to_close={max_sec}',
            '--strategy-param', f'max_ticket_price={max_price}',
            '--strategy-param', f'min_ticket_price={min_price}',
            '--strategy-param', f'side_mode={side_mode}',
            '--strategy-param', f'base_notional={base_notional}',
            '--json',
            '--overwrite',
        ]
        print(f'[{i}/{len(combos)}] {run_id}', flush=True)
        proc = subprocess.run(cmd, cwd=REPO, text=True, capture_output=True, timeout=900)
        row['returncode'] = proc.returncode
        row['stderr_tail'] = proc.stderr[-1000:]
        row['stdout_tail'] = proc.stdout[-1000:]
        if proc.returncode == 0:
            summarize_run(row, run_dir)
        else:
            row['status'] = 'error'
            row['run_dir'] = str(run_dir)
        rows.append(row)
        write_checkpoint(rows)

    sorted_rows = sorted(rows, key=lambda r: float(r.get('realized_pnl') if r.get('realized_pnl') is not None else -10**12), reverse=True)
    payload = {'generated_at': STAMP, 'feed_db': str(FEED_DB), 'rows': rows, 'top_by_pnl': sorted_rows[:20]}
    OUT_JSON.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')
    fields = sorted({k for r in rows for k in r})
    with OUT_CSV.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(rows)
    lines = [f'# Late lotto ticket sweep {STAMP}', '', f'Feed: `{FEED_DB}`', '', '## Top by PnL', '']
    for r in sorted_rows[:25]:
        lines.append(
            '- '
            f"pnl={r.get('realized_pnl')} fills={r.get('fills')} win_rate={r.get('win_rate')} "
            f"maxdd={r.get('max_drawdown')} largest_win={r.get('largest_win')} "
            f"ratio={r.get('largest_win_to_pnl_ratio')} window={r.get('min_seconds_to_close')}-{r.get('max_seconds_to_close')} "
            f"price={r.get('min_ticket_price')}-{r.get('max_ticket_price')} side={r.get('side_mode')} notional={r.get('base_notional')} "
            f"run=`{r.get('run_dir')}`"
        )
    OUT_MD.write_text('\n'.join(lines) + '\n')
    print(json.dumps({'json': str(OUT_JSON), 'csv': str(OUT_CSV), 'md': str(OUT_MD), 'checkpoint': str(CHECKPOINT_JSON), 'runs': len(rows)}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
