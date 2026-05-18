# Strategy tuning notes

These notes capture the replay-tuning state for the inventory-shaping BTC 15m research branch. They are intentionally replay/paper research notes, not live-trading approval.

## Safety boundary

- Strategy work is replay/paper only.
- Replays use `--fill-timing next-tick` and `limit_price` in signal features.
- `seed_cheap_accumulate_repair_v1` now also enforces:
  - `min_order_contracts = 5`
  - `max_net_ratio`
  - `one_fill_per_price_level = true`
- Polymarket fill validation is public orderbook observation only. It refuses `--place-order` and does not include wallet signing or authenticated order placement.

## Strategy variations added

### `complement_ladder_v0`

Lot-based YES/NO complement strategy. It treats completed pairs as the durable unit and keeps one-sided entries small/capped.

Useful for testing strict pair-edge assumptions:

```text
pair_cost = yes_fill_price + no_fill_price
pair_edge = 1.00 - pair_cost
```

### `cheap_accumulate_repair_v0`

Simpler cheap-side accumulation strategy:

1. Buy whichever side is cheap.
2. Respect total dollar cap per 15m market.
3. Stop hunting in repair window.
4. Buy smaller side only to reduce imbalance.

Key replay lesson: pure cheap accumulation found positive paired edge but often over-accumulated one-sided leftovers.

### `seed_cheap_accumulate_repair_v1`

Current best structural candidate. It shifts from “cheap add bot” to “inventory shaping bot”:

1. Opening 3:1 seed in the 12-15m window.
2. Tiny cheap adds.
3. Early repair around 6m remaining.
4. Dominant-side blocking via net-ratio cap.
5. One-fill-per-price-level resting-limit guard.

## Current best config center

Balanced candidate for live-paper style validation:

```text
strategy = seed_cheap_accumulate_repair_v1
seed_primary_spend = 90
seed_hedge_spend = 30
seed_min_seconds_to_close = 720
seed_max_seconds_to_close = 900
cheap_price = 0.15
very_cheap_price = 0.08
normal_spend = 1
very_cheap_spend = 2
max_total_cost = 200
max_net_ratio = 0.35
min_order_contracts = 5
one_fill_per_price_level = true
repair_start_seconds = 360
repair_max_price = 0.85
target_net_ratio = 0.05
no_trade_seconds = 15
min_seconds_between_orders = 5
```

Aggressive variant:

```text
seed_primary_spend = 90
seed_hedge_spend = 30
repair_start_seconds = 360
max_net_ratio = 0.45
```

Smaller/liquidity-safer variant:

```text
seed_primary_spend = 80
seed_hedge_spend = 26.666667
repair_start_seconds = 360
max_net_ratio = 0.40
```

## Replay results snapshot

All results below used next-tick limit replay, official settlements, $200 max cost per 15m market, 5-contract min, and one-fill-per-price-level where noted.

### Best after one-fill-per-price-level realism

Run:

```text
runs/seed_cheap_accumulate_repair_v1/tune_90_30_r360_net0.45-20260518T065339Z
```

Params:

```text
seed_primary_spend = 90
seed_hedge_spend = 30
repair_start_seconds = 360
max_net_ratio = 0.45
```

Metrics:

```text
fills = 1400
notional = 32838.04
cost = 31580.50
realized_pnl = +595.72
completed_pair_pnl = +2127.09
unpaired_leftover_pnl = -1531.37
paired_locked_edge = +2854.42
max_abs_raw_net_contracts = 352.16
profit_factor = 1.041
```

Largest leftover drags:

```text
no:12-15m  cost=1151.68  pnl=-1014.50
yes:12-15m cost=413.25   pnl=-346.15
```

### Balanced lower-imbalance variant

Run:

```text
runs/seed_cheap_accumulate_repair_v1/tune_90_30_r360_net0.35-20260518T065226Z
```

Metrics:

```text
realized_pnl = +548.12
completed_pair_pnl = +2133.58
unpaired_leftover_pnl = -1585.46
max_abs_raw_net_contracts = 285.50
profit_factor = 1.038
```

### Smaller/liquidity-safer variant

Run:

```text
runs/seed_cheap_accumulate_repair_v1/tune_80_26p666667_r360_net0.40-20260518T064759Z
```

Metrics:

```text
realized_pnl = +514.29
completed_pair_pnl = +1855.98
unpaired_leftover_pnl = -1341.70
max_abs_raw_net_contracts = 303.66
profit_factor = 1.039
```

## Structural conclusions

1. `repair_start_seconds = 360` kept winning across sweeps. The market appears to stop being a good accumulation market well before the visible 2m repair point.
2. Larger seeds scaled better up to about 90/30 under a $200 market budget.
3. The tiny cheap adds are no longer the economic core; they are convexity probes. The seed and repair path dominate.
4. Contract count alone is misleading risk measurement because many contracts are low-priced. Use total cost plus net ratio/max raw net together.
5. Replay PnL survived more realistic constraints, but live viability now depends on visible depth, queue position, partial fills, and cancellation behavior.

## Polymarket fill-validation next step

Use the public-orderbook validator before authenticated order tests:

```bash
.venv/bin/python -m kalshibtc.polymarket_fill_validation \
  --token-id <POLYMARKET_CLOB_TOKEN_ID> \
  --side BUY \
  --limit-price 0.51 \
  --contracts 5 \
  --samples 30 \
  --interval-seconds 1 \
  --sqlite-out runs/polymarket-fill-validation.sqlite3 \
  --json
```

The validator compares replay-style touch assumptions against visible CLOB depth:

```text
BUY replay touch:  best_ask <= limit_price
SELL replay touch: best_bid >= limit_price
full visible fill: visible depth at/through limit >= requested contracts
```

Only after this public observation looks reasonable should an authenticated place/wait/cancel 5-contract Polymarket test be added, and that requires explicit live-order approval.
