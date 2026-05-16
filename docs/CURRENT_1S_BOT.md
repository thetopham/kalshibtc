# Current Feed/Replay System

The active code is the feed/replay package under `src/kalshibtc/`.

## Goal

Keep the system small and inspectable:

```text
read-only feed -> SQLite tape -> strategy replay/backtest -> immutable run outputs
```

No active live-order service or old 15m scanner package is required for this workflow.

## Canonical paths

```text
feed/kalshi-btc-1s.sqlite3
runs/
logs/
```

`data/`, `data-live-prod/`, and old `paper-ledger.sqlite3` files are historical artifacts. Do not use them for new services or docs.

## Commands

```bash
kbtc-feed --snapshot-db feed/kalshi-btc-1s.sqlite3 --loop --emit-min-interval-seconds 1

kbtc-replay --feed-db feed/kalshi-btc-1s.sqlite3 --runs-dir runs --strategy simple_directional

kbtc-dashboard --snapshot-db feed/kalshi-btc-1s.sqlite3 --results-db runs/latest/results.sqlite3 --host 127.0.0.1 --port 8792
```

## Active scripts

```text
kbtc-feed      -> kalshibtc.record_1s_snapshots:main
kbtc-replay    -> kalshibtc.replay.cli:main
kbtc-dashboard -> kalshibtc.dashboard:main
```

## Active flow

```text
feed row from recorder
  -> MarketState
  -> selected Strategy emits Signal
  -> RiskManager sizes or blocks
  -> PaperExecutor creates simulated fill during replay
  -> runs/<strategy>/<run-id>/results.sqlite3 + metrics.json
```

## Strategy boundary

Strategy code belongs in `src/kalshibtc/strategy/` and should be pure:

```python
class Strategy:
    def on_tick(self, state) -> Signal:
        ...
```

Strategies must not read/write SQLite, submit orders, or call network APIs.

## Websocket status

The active recorder CLI currently writes the canonical feed schema. The package-local websocket adapter seam is:

```text
src/kalshibtc/datafeed/websocket.py
```

The old websocket implementation is archived at:

```text
archive/legacy-15m/src/kalshi_btc_15m_bot/streaming.py
```

If websocket work resumes, port only the read-only feed parts into `src/kalshibtc/datafeed/`:

- Coinbase ticker parser/loop
- Kalshi websocket URL/auth/header helper
- Kalshi orderbook_delta parser/loop
- reconnect/backoff logic
- normalized snapshot writer handoff

Do not port old live-order, scanner, model-probability, Supabase, or dashboard coupling.

## Verification

```bash
uv run pytest tests/test_feed_replay_architecture.py -q
uv run pytest
uv run python -m compileall src tests
uv run ruff check .
```
