# Current 1s Bot

This repo's active code is the boring 1-second Kalshi BTC paper bot under `src/kalshibtc/`.

Current question:

> Does simple slope + above/below have edge, and where, or is it just noise?

Do not add ML, market making, EV blending, new indicators, dashboards, live orders, or strategy complexity until paper PnL review shows evidence for a specific next change.

## Active entrypoints

Recorder:

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

Paper executor:

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

The active installed script aliases are `kbtc15-1s-recorder` and `kbtc15-1s-paper`.

Recorder v1 is deliberately small and read-only: it polls public Coinbase BTC spot plus Kalshi public market/orderbook endpoints, then writes compatible SQLite rows. A later source swap can put an authenticated read-only Kalshi websocket behind the same `realtime_snapshots_1s` writer without restoring the legacy CLI.

## Database split

Keep these databases separate:

```text
data/realtime-snapshots-1s.sqlite3
  Recorder-owned stream DB.
  Stores normalized 1s BTC/Kalshi snapshots in `realtime_snapshots_1s`.
  `kbtc15-1s-recorder` creates/upserts this table.
  The paper executor only SELECTs from this DB.

data-live-prod/paper-results-1s.sqlite3
  Paper-executor results DB.
  Stores generated signals, fake fills, exits, and PnL.
```

Do not write paper results into the stream tape.

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

## Flow and ownership

```text
snapshot row from recorder
  -> MarketState
  -> SimpleDirectionalStrategy emits Signal
  -> RiskManager sizes or blocks
  -> PaperExecutor creates fake fill
  -> PaperSignalStore writes prediction/trade/settlement rows
```

Important boundary:

- Recorder creates/upserts stream snapshots only.
- Strategy emits `Signal`.
- Risk sizes or blocks.
- Paper executor creates fake fills only.
- Storage reads snapshots and writes results.
- No live order path is constructed by `paper_signal_executor.py`.

## Active module map

```text
src/kalshibtc/
  __init__.py
  config.py                    # BotConfig and RiskLimits defaults for the modular path
  main.py                      # MarketStateBuilder, BotPipeline, strategy -> risk -> executor wiring
  record_1s_snapshots.py       # read-only active recorder CLI into realtime_snapshots_1s
  paper_signal_executor.py     # thin CLI/live 1s paper loop over snapshot DB into results DB

  datafeed/
    models.py                  # Tick and OrderBookSnapshot
    websocket.py               # live feed protocol boundary
    recorder.py                # lightweight SQLite snapshot writer seam

  market/
    contract.py                # ContractWindow, strike/close metadata
    kalshi_public.py           # read-only unauthenticated official settlement lookup
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
    paper_signal_store.py      # 1s snapshot/results repository and paper settlement persistence
    schema.sql                 # modular storage schema
```

## Archived legacy code

Everything that is not active 1s bot code has been moved under:

```text
archive/legacy-15m/
```

That archive contains the old `kalshi_btc_15m_bot` package, old tests, legacy configs, old deploy templates, and historical notes. It is reference only and is excluded from active packaging/linting.

If something from the archive becomes necessary again, move only the small needed piece into `src/kalshibtc/` behind a tested 1s-specific seam. Do not re-expand the archive in place.

## Paper PnL review before complexity

Review `data-live-prod/paper-results-1s.sqlite3` by:

- total PnL
- win rate
- average PnL
- PnL by side
- PnL by seconds-to-expiry bucket
- PnL by distance-from-strike bucket
- PnL by slope-at-entry bucket
- PnL by hold-time bucket

Only after that review should new complexity be considered.

## Tests and checks

Focused modular tests:

```bash
uv run pytest tests/test_kalshibtc_1s_recorder.py -q
uv run pytest tests/test_kalshibtc_modular_core.py -q
```

Full local checks:

```bash
uv run python -m kalshibtc.record_1s_snapshots --help
uv run python -m kalshibtc.paper_signal_executor --help
uv run kbtc15-1s-recorder --help
uv run kbtc15-1s-paper --help
uv run pytest
uv run python -m compileall src tests
uv run ruff check .
```
