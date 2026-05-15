# 1s Modular Kalshi BTC Bot Architecture

Companion doc: [CURRENT_1S_BOT.md](CURRENT_1S_BOT.md).

The active package is now only:

```text
src/kalshibtc/
```

The old 15m scanner/model/dashboard/live-order code is archived under `archive/legacy-15m/` and is not part of the active import path.

## Core flow

```text
1s snapshot row
   ↓
market state builder
   ↓
slope / analyzer module
   ↓
SimpleDirectionalStrategy
   ↓
risk filter / position sizing
   ↓
paper executor
   ↓
results database
```

Current strategy/risk/replay work stops at paper execution. Any real-order adapter stays disabled until the paper evidence review justifies a separate live-safety task.

Strategy logic must not submit orders. Execution adapters must not decide strategy.

## Package map

```text
src/kalshibtc/
  paper_signal_executor.py      # CLI loop over snapshot DB -> results DB
  main.py                       # wires MarketStateBuilder + strategies + risk + executor
  config.py                     # symbols, DB paths, risk limits

  datafeed/
    models.py                   # Tick, OrderBookSnapshot
    websocket.py                # protocol boundary for future live 1s feed adapters
    recorder.py                 # lightweight SQLite snapshot writer seam

  market/
    contract.py                 # strike, close time, above/below logic
    kalshi_public.py            # unauthenticated read-only public settlement lookup
    pricing.py                  # bid/ask/spread helpers
    state.py                    # normalized per-tick MarketState

  strategy/
    signals.py                  # Signal + Strategy protocol
    slope.py                    # windowed BTC velocity
    simple_directional.py       # price-vs-strike + 30s slope rule

  execution/
    risk.py                     # risk gates, sizing, blockers
    paper.py                    # fake fills only
    kalshi.py                   # disabled-by-default real-order boundary
    position.py                 # reusable exit/hold rule seam

  storage/
    db.py                       # generic SQLite helpers
    paper_signal_store.py       # 1s executor snapshot/results repository
    schema.sql                  # ticks/signals/fills tables for modular seams

  backtest/
    replay.py                   # replay 1s ticks/books through same pipeline
    metrics.py                  # win rate, EV/trade, drawdown
```

## Strategy interface

The first strategy interface is deliberately boring:

```python
class Strategy:
    def on_tick(self, state) -> Signal:
        ...
```

Signal shape:

```python
@dataclass(frozen=True)
class Signal:
    side: str          # "long_above", "long_below", "none"
    reason: str
    confidence: float
    strategy: str = "manual"
```

Current rule:

```python
if price > strike and slope_30s > 0:
    return Signal("long_above", "above strike + trend up", confidence)

if price < strike and slope_30s < 0:
    return Signal("long_below", "below strike + trend down", confidence)

return Signal("none", "no alignment", 0)
```

## Paper review loop

Do not add strategy complexity until the simple slope + above/below rule has enough paper evidence. Keep the live 1s data stream and paper results in separate databases:

```text
data/realtime-snapshots-1s.sqlite3       # recorder-owned stream DB; executor only SELECTs
data-live-prod/paper-results-1s.sqlite3  # paper executor results DB; signals/fake fills/exits/PnL
```

Paper executor command:

```bash
python -m kalshibtc.paper_signal_executor \
  --snapshot-db data/realtime-snapshots-1s.sqlite3 \
  --results-db data-live-prod/paper-results-1s.sqlite3 \
  --loop --interval-seconds 1
```

Review results by side, seconds-to-expiry bucket, distance-from-strike bucket, slope-at-entry bucket, and hold-time bucket before changing strategy logic.

## Safety boundary

- `kalshibtc.execution.paper.PaperExecutor` only creates fake fills.
- `kalshibtc.execution.kalshi.KalshiBrokerAdapter` returns `live_orders_disabled` by default and does not submit orders.
- `paper_signal_executor.py` does not construct a live-order client.
- Archived live-order code remains reference only under `archive/legacy-15m/`.

## Testing

Focused modular seam tests:

```bash
uv run pytest tests/test_kalshibtc_modular_core.py -q
```

Full verification:

```bash
uv run pytest
uv run python -m compileall src tests
uv run ruff check .
```
