# kalshibtc — Kalshi BTC feed and strategy replay lab

## Current focus

This repo is now a feed/research system:

1. record a read-only Kalshi BTC 1s feed into one canonical SQLite tape
2. keep process logs separate from data
3. replay/backtest strategies against that feed into immutable run directories
4. keep dashboards read-only

No active live-order package or `kbtc15` console script remains in the active import path. Historical 15m/live/scanner code is reference-only under `archive/legacy-15m/`.

## Canonical runtime layout

```text
feed/kalshi-btc-1s.sqlite3      # authoritative 1s feed tape
runs/                           # replay/backtest outputs, one immutable dir per run
logs/                           # process logs only
```

Avoid using `data/` or `data-live-prod/` for new work. Existing files there are historical compatibility artifacts.

## Active commands

Record the feed:

```bash
kbtc-feed \
  --snapshot-db feed/kalshi-btc-1s.sqlite3 \
  --emit-min-interval-seconds 1 \
  --loop
```

Replay a strategy against the feed:

```bash
kbtc-replay \
  --feed-db feed/kalshi-btc-1s.sqlite3 \
  --runs-dir runs \
  --strategy simple_directional
```

Run the read-only dashboard:

```bash
kbtc-dashboard \
  --snapshot-db feed/kalshi-btc-1s.sqlite3 \
  --results-db runs/latest/results.sqlite3 \
  --host 127.0.0.1 --port 8792
```

## Strategy/replay model

Strategies live in `src/kalshibtc/strategy/` and should remain pure:

```text
MarketState -> Signal
```

Replay reads only from the feed DB and writes outputs under:

```text
runs/<strategy>/<run-id>/
  config.toml
  metrics.json
  results.sqlite3
```

The initial strategy remains deliberately boring:

```python
if price > strike and slope_30s > 0:
    long_above
elif price < strike and slope_30s < 0:
    long_below
else:
    no_trade
```

## Active package map

```text
src/kalshibtc/
  record_1s_snapshots.py       # read-only feed recorder CLI; writes realtime_snapshots_1s
  replay/cli.py                # feed DB -> immutable run outputs
  dashboard.py                 # read-only dashboard
  runtime_paths.py             # canonical feed/runs/log paths
  datafeed/                    # feed models and websocket boundary
  market/                      # contract, state, pricing, public settlement lookup
  strategy/                    # Signal protocol and strategy implementations
  execution/                   # paper fill/risk seams only
  storage/                     # SQLite helpers/result store compatibility
  backtest/                    # replay engine/metrics primitives
```

`src/kalshibtc/datafeed/websocket.py` is still the seam for a package-local live websocket adapter. The old websocket implementation is available as reference in `archive/legacy-15m/src/kalshi_btc_15m_bot/streaming.py`; port only the feed/auth/parser pieces, not live-order or dashboard code.

## Safety boundary

- Feed recorder writes local feed observations only.
- Replay/backtest reads feed observations and writes run outputs only.
- Dashboard is read-only.
- Active package must not import `kalshi_btc_15m_bot`.
- Live-order/scanner code is archive-only reference.

## Checks

```bash
uv run kbtc-feed --help
uv run kbtc-replay --help
uv run kbtc-dashboard --help
uv run pytest
uv run python -m compileall src tests
uv run ruff check .
```
