# Current 1s Bot

This is the current target for this repo: a boring 1-second, websocket-first Kalshi BTC paper bot for `KXBTC15M` markets.

The current question is deliberately narrow:

> Does simple slope + above/below have edge, and where, or is it just noise?

Do not add ML, market making, EV blending, new indicators, live orders, or strategy complexity until paper PnL review shows evidence that the simple baseline has a specific weakness worth fixing.

## North star flow

```text
1s datafeed
  -> market state builder
  -> slope/analyzer
  -> strategy
  -> risk/position sizing
  -> paper executor
  -> logger/database/dashboard/stats
```

Important boundary:

- Strategy emits `Signal`.
- Risk sizes or blocks.
- Executor fills/submits.
- Strategy logic must not submit orders.
- Execution adapters must not decide strategy.

## Current entrypoints

Collect the 1s tape:

```bash
kbtc15 --config configs/default.toml record-1s --emit-min-interval-seconds 1
```

Run the 1s paper executor:

```bash
python -m kalshibtc.paper_signal_executor \
  --snapshot-db data/realtime-snapshots-1s.sqlite3 \
  --results-db data-live-prod/paper-results-1s.sqlite3 \
  --loop --interval-seconds 1
```

The recorder command is still exposed through the `kbtc15` CLI while the current strategy/risk/execution/replay seams live in `src/kalshibtc/`.

## Database split

Keep these databases separate:

```text
data/realtime-snapshots-1s.sqlite3
  Recorder-owned stream DB.
  Stores normalized 1s BTC/Kalshi snapshots in `realtime_snapshots_1s`.
  The paper executor should only SELECT from this DB.

data-live-prod/paper-results-1s.sqlite3
  Paper-executor results DB.
  Stores generated signals, fake fills, exits, and PnL.
```

Do not write paper results into the stream tape. Do not make the recorder depend on paper results.

## V1 strategy rule

Current strategy: `SimpleDirectionalStrategy` in `src/kalshibtc/strategy/simple_directional.py`.

```python
if price > strike and slope_30s > 0:
    long_above

elif price < strike and slope_30s < 0:
    long_below

else:
    no_trade
```

The baseline is intentionally boring. It exists so paper results can answer whether the simplest above/below plus slope alignment has any useful edge before the repo grows more moving parts.

## Module map

```text
src/kalshibtc/
  __init__.py
  config.py                    # BotConfig and RiskLimits defaults for the modular path
  main.py                      # MarketStateBuilder, BotPipeline, strategy -> risk -> executor wiring
  paper_signal_executor.py     # thin CLI/live 1s paper loop over snapshot DB into results DB

  datafeed/
    models.py                  # Tick and OrderBookSnapshot
    websocket.py               # live feed protocol boundary; wraps legacy streaming later, not in paper executor
    recorder.py                # SQLite recorder seam for feed snapshots

  market/
    contract.py                # ContractWindow, strike/close metadata
    kalshi_public.py           # read-only unauthenticated official settlement lookups
    pricing.py                 # quote/spread helpers
    state.py                   # normalized MarketState used by strategies/risk

  strategy/
    signals.py                 # Signal dataclass and Strategy protocol
    slope.py                   # rolling BTC slope tracker
    simple_directional.py      # current v1 strategy

  execution/
    risk.py                    # RiskManager and sizing/blockers
    paper.py                   # fake fills only
    kalshi.py                  # disabled-by-default future live boundary
    position.py                # reusable position/exit rule seam

  backtest/
    replay.py                  # replay ticks/books through the same strategy/risk/paper seams
    metrics.py                 # basic trade metrics

  storage/
    db.py                      # generic SQLite helpers
    paper_signal_store.py      # 1s executor snapshot/results repositories and paper settlement persistence
    schema.sql                 # modular storage schema
```

## Legacy coupling status

Decoupled in this refactor:

- `python -m kalshibtc.paper_signal_executor` no longer imports `kalshi_btc_15m_bot.kalshi_client`.
- The official settlement lookup used by the 1s paper path now lives in `src/kalshibtc/market/kalshi_public.py` as a small unauthenticated read-only client.
- Snapshot reads, cursor tracking, prediction writes, fake-fill persistence, and settlement/PnL writes now live behind `PaperSignalStore` in `src/kalshibtc/storage/paper_signal_store.py`.
- `paper_signal_executor.py` stays focused on orchestration: snapshot row -> `MarketState` -> `SimpleDirectionalStrategy` -> `RiskManager` -> `PaperExecutor` -> results DB.

Remaining known legacy boundaries:

- The recorder entrypoint is still exposed by the legacy `kbtc15` CLI.
- `src/kalshibtc/datafeed/websocket.py` is a protocol/placeholder that explicitly points at wrapping `kalshi_btc_15m_bot.streaming` later.
- The legacy package remains available for the older scanner, dashboard/reporting pieces, and migration reference.

Refactor later:

- Move or wrap the active 1s recorder entrypoint into `src/kalshibtc/` without changing the live stream behavior.
- Review dashboard/reporting dependencies and decide which read-only paper-PnL views belong in the modular package.
- Keep the legacy authenticated/live-order adapter out of the 1s paper executor until live trading is explicitly redesigned and approved.

## Paper PnL review before complexity

The next real task is to review `data-live-prod/paper-results-1s.sqlite3` by:

- total PnL
- win rate
- average PnL
- PnL by side
- PnL by seconds-to-expiry bucket
- PnL by distance-from-strike bucket
- PnL by slope-at-entry bucket
- PnL by hold-time bucket

Only after that review should new complexity be considered. If the baseline is noise, document that. If edge exists only in a bucket, constrain the next change to that evidence.

## Tests and checks

Focused modular tests:

```bash
pytest tests/test_kalshibtc_modular_core.py -q
```

Full local checks:

```bash
pytest
python -m compileall src tests
ruff check .
```

## What not to touch yet

- Do not enable live orders.
- Do not add ML, market making, EV blending, or new indicators.
- Do not merge `src/kalshi_btc_15m_bot/` with `src/kalshibtc/`.
- Do not delete legacy code.
- Do not move new strategy work into the legacy package.
- Do not add strategy complexity until paper PnL review shows evidence.
