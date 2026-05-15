# kalshibtc — boring 1s Kalshi BTC paper bot

## Current focus

This repo is now narrowed to the 1-second Kalshi BTC paper bot in `src/kalshibtc/`.

Active question:

> Does simple BTC price-vs-strike plus 30s slope have edge, and where, or is it just noise?

Do not add ML, market making, EV blending, new indicators, dashboards, live orders, or strategy complexity until paper PnL review shows a specific weakness worth fixing.

## Active command

Run the 1s paper executor over an existing recorder-owned snapshot DB:

```bash
python -m kalshibtc.paper_signal_executor \
  --snapshot-db data/realtime-snapshots-1s.sqlite3 \
  --results-db data-live-prod/paper-results-1s.sqlite3 \
  --loop --interval-seconds 1
```

Equivalent installed script:

```bash
kbtc15-1s-paper \
  --snapshot-db data/realtime-snapshots-1s.sqlite3 \
  --results-db data-live-prod/paper-results-1s.sqlite3 \
  --loop --interval-seconds 1
```

The executor only SELECTs from the snapshot DB and writes signals, fake fills, exits, and PnL to the separate results DB.

## V1 strategy

`SimpleDirectionalStrategy` remains deliberately boring:

```python
if price > strike and slope_30s > 0:
    long_above
elif price < strike and slope_30s < 0:
    long_below
else:
    no_trade
```

Strategy emits `Signal`. `RiskManager` sizes or blocks. `PaperExecutor` creates fake fills. No live order code is in the paper executor path.

## Active package map

```text
src/kalshibtc/
  paper_signal_executor.py       # thin 1s paper loop CLI
  main.py                        # MarketStateBuilder and strategy/risk/executor pipeline
  config.py                      # small 1s defaults
  datafeed/                      # feed models and lightweight recorder seam
  market/                        # contract, state, pricing, read-only public settlement client
  strategy/                      # Signal, slope tracker, SimpleDirectionalStrategy
  execution/                     # RiskManager, PaperExecutor, disabled live boundary
  storage/                       # SQLite schema/helpers and PaperSignalStore
  backtest/                      # replay/metrics seams
```

## Archived legacy code

The old scanner/model/dashboard/live-order surface has been moved out of the active package path:

```text
archive/legacy-15m/
```

That archive includes the old `kalshi_btc_15m_bot` package, old tests, configs, deploy templates, and historical notes. Treat it as reference only. If a piece is needed again, move only that small piece into `src/kalshibtc/` behind tests.

## Paper PnL review before complexity

Review `data-live-prod/paper-results-1s.sqlite3` before adding strategy complexity:

- total PnL
- win rate
- average PnL
- PnL by side
- PnL by seconds-to-expiry bucket
- PnL by distance-from-strike bucket
- PnL by slope-at-entry bucket
- PnL by hold-time bucket

## Checks

This host currently uses `uv run` because bare `python`/`pytest` may not be on PATH:

```bash
uv run python -m kalshibtc.paper_signal_executor --help
uv run pytest tests/test_kalshibtc_modular_core.py -q
uv run pytest
uv run python -m compileall src tests
uv run ruff check .
```
