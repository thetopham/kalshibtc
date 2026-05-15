# 1s Modular Kalshi BTC Bot Architecture

Companion docs:

- [CURRENT_1S_BOT.md](CURRENT_1S_BOT.md) is the one-page operator/dev overview for the current boring 1s bot.
- [LEGACY.md](LEGACY.md) explains the old `src/kalshi_btc_15m_bot/` path and why new strategy work should not go there.

This repo now has two bot lines:

1. **Legacy 1-minute scanner** — the original `kalshi_btc_15m_bot` CLI path (`scan`, `run`, predictor, live guards). It has useful ledger, dashboard, and live-order guardrail code, but also accumulated feature creep.
2. **1-second websocket-first core** — the new boring `kalshibtc` package. This is the target shape for data collection, strategy development, replay/backtesting, paper execution, and clean adapter seams. Live adapters remain disabled/out of scope for the current evidence-gathering loop.

The near-term goal is not to delete the legacy scanner. The goal is to stop adding strategy/execution complexity to it and move new work into simple seams that can be tested offline.

## Core flow

```text
1s websocket datafeed
   ↓
market state builder
   ↓
slope / analyzer module
   ↓
strategy module(s)
   ↓
risk filter / position sizing
   ↓
paper executor
   ↓
logger + database + dashboard/reports/stats
```

Current strategy/risk/replay work stops at paper execution. Any real-order adapter stays disabled until the paper evidence review justifies a separate live-safety task.

Strategy logic must not submit orders. Execution adapters must not decide strategy.

## Package map

```text
src/kalshibtc/
  main.py                 # wires MarketStateBuilder + strategies + risk + executor
  config.py               # symbols, DB paths, risk limits

  datafeed/
    models.py             # Tick, OrderBookSnapshot
    websocket.py          # protocol boundary for live 1s feed adapters
    recorder.py           # SQLite snapshot writer

  market/
    contract.py           # strike, close time, above/below logic
    pricing.py            # bid/ask/spread helpers
    state.py              # normalized per-tick MarketState

  strategy/
    signals.py            # Signal + Strategy protocol
    slope.py              # windowed BTC velocity
    simple_directional.py # price-vs-strike + 30s slope rule

  execution/
    risk.py               # risk gates, sizing, blockers
    paper.py              # fake fills only
    kalshi.py             # disabled-by-default real-order boundary
    position.py           # reusable exit/hold rule seam

  backtest/
    replay.py             # replay 1s ticks/books through same pipeline
    metrics.py            # win rate, EV/trade, drawdown

  storage/
    db.py                 # SQLite helpers
    schema.sql            # ticks/signals/fills tables
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

## Multiple strategies

`BotPipeline` accepts a list of strategies and runs each strategy against the same `MarketState`:

```python
pipeline = BotPipeline(
    strategies=[SimpleDirectionalStrategy(name="fast"), OtherStrategy(name="slow")],
    risk_manager=RiskManager(RiskLimits(base_size_dollars=10)),
    executor=PaperExecutor(),
)
results = pipeline.on_state(state)
```

That makes the intended development loop:

- collect 1s data once;
- replay many strategies over the same tape;
- paper trade multiple strategies in parallel;
- compare stats without rewriting feed or execution code.

## Paper review loop

Do not add strategy complexity until the simple slope + above/below rule has enough paper evidence. Keep the live 1s data stream and paper results in separate databases:

```text
data/realtime-snapshots-1s.sqlite3       # recorder-owned stream DB; recorder keeps adding 1s rows
data-live-prod/paper-results-1s.sqlite3  # paper executor results DB; signals/fake fills/exits/PnL
```

The paper executor does not chmod the stream DB, does not open it in read-only file mode, and does not write result tables into it. It only SELECTs from the stream tape and writes all generated research results to the separate results DB:

```bash
python -m kalshibtc.paper_signal_executor \
  --snapshot-db data/realtime-snapshots-1s.sqlite3 \
  --results-db data-live-prod/paper-results-1s.sqlite3 \
  --loop --interval-seconds 1
```

Run the 1s collector/paper path and persist:

1. every 1s BTC tick;
2. Kalshi order book snapshots;
3. generated strategy signals;
4. fake fills;
5. exits and PnL.

The `/status` dashboard exposes a **Paper PnL Review** table with these review columns: strategy, side, entry time, exit time, entry price, exit price, PnL, hold seconds, slope at entry, distance from strike, seconds to expiry, and reason. It also exposes **Paper Review Buckets** grouped by strategy, side, seconds-to-expiry bucket, distance-from-strike bucket, slope-at-entry bucket, and hold-seconds bucket. Each bucket reports trades, win rate, average PnL, total PnL, and average hold seconds. The operator question is: does simple slope + above/below actually have edge, and where, or is it just noise?

## Safety boundary

- The new `kalshibtc.execution.paper.PaperExecutor` only creates fake fills.
- The new `kalshibtc.execution.kalshi.KalshiBrokerAdapter` returns `live_orders_disabled` by default and does not submit orders.
- Existing guarded live-order code remains in the legacy package and is still opt-in only.
- Do not enable live orders until a strategy has been replayed, paper-tested, and reviewed against risk limits.

## Testing

Focused modular seam tests live in `tests/test_kalshibtc_modular_core.py`:

```bash
pytest tests/test_kalshibtc_modular_core.py -q
```

Full verification remains:

```bash
pytest
python -m compileall src tests
ruff check .
```
