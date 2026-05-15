# kalshibtc — boring 1s Kalshi BTC paper bot

## Current focus

This repo is now narrowed to the 1-second Kalshi BTC paper bot in `src/kalshibtc/`.

Active question:

> Does simple BTC price-vs-strike plus 30s slope have edge, and where, or is it just noise?

Do not add ML, market making, EV blending, new indicators, dashboards, live orders, or strategy complexity until paper PnL review shows a specific weakness worth fixing.

## Active commands

Run the read-only 1s recorder to keep the snapshot DB fresh:

```bash
kbtc15-1s-recorder \
  --snapshot-db data/realtime-snapshots-1s.sqlite3 \
  --emit-min-interval-seconds 1 \
  --loop
```

Equivalent module form:

```bash
python -m kalshibtc.record_1s_snapshots \
  --snapshot-db data/realtime-snapshots-1s.sqlite3 \
  --emit-min-interval-seconds 1 \
  --loop
```

Then run the 1s paper executor over that recorder-owned snapshot DB:

```bash
kbtc15-1s-paper \
  --snapshot-db data/realtime-snapshots-1s.sqlite3 \
  --results-db data-live-prod/paper-results-1s.sqlite3 \
  --loop --interval-seconds 1
```

Equivalent module form:

```bash
python -m kalshibtc.paper_signal_executor \
  --snapshot-db data/realtime-snapshots-1s.sqlite3 \
  --results-db data-live-prod/paper-results-1s.sqlite3 \
  --loop --interval-seconds 1
```

The recorder writes only `realtime_snapshots_1s` rows to the snapshot DB. The executor only SELECTs from the snapshot DB and writes signals, fake fills, exits, and PnL to the separate results DB.

Recorder v1 is a tiny read-only public-data poller: Coinbase BTC spot plus Kalshi public market/orderbook endpoints. It has no credentials and no order-submission path; a later PR can swap in a read-only websocket source behind the same SQLite writer.

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
  record_1s_snapshots.py          # active read-only recorder CLI for realtime_snapshots_1s
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
uv run python -m kalshibtc.record_1s_snapshots --help
uv run python -m kalshibtc.paper_signal_executor --help
uv run kbtc15-1s-recorder --help
uv run kbtc15-1s-paper --help
uv run pytest tests/test_kalshibtc_1s_recorder.py -q
uv run pytest tests/test_kalshibtc_modular_core.py -q
uv run pytest
uv run python -m compileall src tests
uv run ruff check .
```
