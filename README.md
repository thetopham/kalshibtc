# Kalshi BTC 15m Bot

A paper-first Python bot for Kalshi's `KXBTC15M` BTC Up/Down 15-minute markets, with an explicit guarded live adapter for tiny IOC limit orders once you opt in.

Safety boundary: default config is paper-only. Live trading requires a separate live config, Kalshi credentials outside the repo, a literal acknowledgement string, demo/production environment selection, hard dollar/contract caps, cash reserve gates, and SQLite audit logging before any order path is constructed.

## Current direction: 1s websocket-first, boring modules

There are currently two bot lines in this repo:

- **Legacy 1-minute scanner**: the original `kalshi_btc_15m_bot` path (`scan`, `run`, predictor/model blend, ledger, dashboard, guarded live adapter). It still works, but it accumulated research/dashboard feature creep.
- **1-second websocket bot**: the current target. It should collect a clean 1 Hz BTC/Kalshi tape, run simple strategy/analyzer modules, backtest or paper trade multiple strategies at once, and keep execution separate from signal generation.

New strategy work should happen in the boring modular package at `src/kalshibtc/`, documented in [docs/1s-modular-architecture.md](docs/1s-modular-architecture.md). The intended core flow is:

```text
1s datafeed → market state builder → slope/analyzer → strategy → risk/position sizing → paper/live adapter → logger/database/dashboard/stats
```

The important boundary is: **strategies emit `Signal`; risk sizes/blocks; executors fill/submit.** Strategy logic must be reusable in live paper mode, historical replay mode, and a future real Kalshi adapter without rewriting the strategy.

## What it does

- Finds the current Kalshi BTC 15-minute up/down market from the public API.
- Pulls BTC 15-minute candles from Coinbase by default, with optional Binance REST support.
- Builds features from the source series: returns, SMA trend, momentum, mean reversion z-scores, volatility, and volume.
- Trains a modest logistic-regression direction model on recent 15-minute BTC candles using time-ordered train/test splits.
- Blends the ML probability with transparent rule-based logic and the live distance to the Kalshi target price.
- Compares predicted probability to Kalshi YES/NO asks and records a local paper trade only when edge gates pass.
- Provides a read-only `stream-state` websocket loop that keeps BTC ticks and the Kalshi order book fresh, recomputes contract-close-aware YES/NO probabilities against the current top-of-book, rolls to the next 15-minute contract after close, and flags model/market/direction disagreement in every state payload.
- Provides a read-only `record-1s` websocket recorder that stores one normalized realtime snapshot per market/second for replay, labeling, and post-session research.
- Provides a `kbtc15-1s-paper` / `python -m kalshibtc.paper_signal_executor` loop that paper-trades the 1s snapshot tape into a separate results database. It SELECTs from the recorder-owned stream DB but writes only to `paper-results-1s.sqlite3`.
- Stores legacy scanner predictions and paper trades in `data/paper-ledger.sqlite3`; stores 1s paper research results separately in `data-live-prod/paper-results-1s.sqlite3` or another explicit `--results-db` path.
- Actively manages paper positions with take-profit, stop-loss, and near-close simulated exits at public bid marks.
- Reports open paper positions with public Kalshi mark-to-market quotes, unrealized PnL, liquidity, max-win exposure, and exit signals.
- Logs performance stats: equity, realized/unrealized PnL, win rate, expectancy, ROI on risk, and largest win/loss.
- Can run continuously with bounded smoke mode, transient provider-error retries, or an unbounded paper-only/live service command.
- Can try to settle open paper trades by reading Kalshi's public market result after settlement.
- Supports authenticated read-only Kalshi checks before trading: balance, portfolio value, and nonzero positions.
- Supports guarded demo/live order submission with RSA-PSS request signing, IOC limit buys, hard order/account caps, min-cash reserve, spread/liquidity gates, duplicate-order cooldowns, and a separate live SQLite audit ledger.
- Manages live positions from confirmed fills and can submit reduce-only IOC exits for take-profit, stop-loss, and near-close time exits.

Important settlement caveat: Kalshi crypto markets settle on CF Benchmarks BRTI, averaged over the final 60 seconds. Coinbase/Binance prices are only proxies. This bot treats them as directional inputs, not the official settlement feed.

## Source-thread takeaways used

The linked @sopersone series was distilled into this implementation:

- Reproducible Python project and isolated environment.
- Data first: fetch, clean, persist OHLCV candles before strategy work.
- Numerical strategy rules only: no vague discretionary signals.
- SMA crossover, momentum, and mean-reversion feature families.
- Avoid look-ahead bias: features at candle t predict candle t+1.
- Backtest before running: metrics are a filter, not proof of future profit.
- Event-driven/paper accounting: commissions, slippage, fills, and state matter. For Kalshi v1, state is a local binary-contract paper ledger.
- ML expectations stay modest: 52-57% directional accuracy can be useful, while 99% is a red flag for overfitting.

One supplied X link appears to be about OpenClaw skills/security rather than the trading-bot series; it did not affect trading logic.

## Setup

```bash
cd /home/matt/workspace/kalshi-btc-15m-bot
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

The default config is `configs/default.toml`. It is paper-only: `trading_mode = "paper"` and `enable_live_orders = false`.

A guarded demo-live template is available at `configs/live-demo.example.toml`. Copy it to an untracked local file before use.

## Commands

```bash
# One-shot prediction scan; may record a paper trade if gates pass
kbtc15 --config configs/default.toml scan

# Continuous paper-only loop; Ctrl-C stops it
kbtc15 --config configs/default.toml run --interval-seconds 60

# Read-only websocket market-state stream; emits fresh BTC/orderbook YES/NO edge state
# Output uses monitor=EDGE_* labels, not order-submission language.
# Kalshi WebSocket requires KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_FILE even when not submitting orders.
kbtc15 --config configs/default.toml stream-state --emit-min-interval-seconds 1

# Read-only 1s websocket recorder; writes normalized snapshots to SQLite for replay/research
kbtc15 --config configs/default.toml record-1s --emit-min-interval-seconds 1

# 1s paper executor; reads the recorder-owned stream DB and writes only to a separate results DB
python -m kalshibtc.paper_signal_executor \
  --snapshot-db data/realtime-snapshots-1s.sqlite3 \
  --results-db data-live-prod/paper-results-1s.sqlite3 \
  --loop --interval-seconds 1

# Legacy websocket paper trader; kept for compatibility with the original ledger path
# Boundary remains paper-only: no Kalshi live orders are submitted by this command.
kbtc15 --config configs/default.toml stream-paper --emit-min-interval-seconds 1

# Bounded loop for smoke testing service behavior
kbtc15 --config configs/default.toml run --interval-seconds 1 --max-scans 2

# JSON output for automation
kbtc15 --config configs/default.toml --json scan

# Local paper ledger status with open-position mark-to-market when Kalshi public quotes are reachable
kbtc15 --config configs/default.toml status

# Operator report with equity, PnL, win rate, expectancy, and open-position exit signals
kbtc15 --config configs/default.toml report

# Read-only stream dashboard; local-only by default and never submits orders
kbtc15 --config configs/default.toml dashboard --host 127.0.0.1 --port 8792 --stream --stream-emit-min-interval-seconds 1

# Try settling open paper trades from public Kalshi results
kbtc15 --config configs/default.toml resolve

# View current KXBTC15M market metadata
kbtc15 --config configs/default.toml markets

# Offline directional backtest on recent 15m BTC candles
kbtc15 --config configs/default.toml backtest
```

## Dashboard

The dashboard is read-only. With `--stream`, it starts the same websocket BTC/orderbook state collector used by `stream-state`, keeps the latest payload in memory, and serves a live browser dashboard at `/` plus JSON at `/api/stream`. BTC and Kalshi websocket drops are expected in long sessions; the stream loop reconnects, and the dashboard collector restarts itself after fatal stream errors while surfacing staleness/error text in the Execution Decision card. The old ledger/account status page remains available at `/status` and `/api/dashboard`. No dashboard route can scan, submit, cancel, or exit orders.

```bash
# Local-only dashboard, no token required because it binds to loopback
kbtc15 --config configs/default.toml dashboard --host 127.0.0.1 --port 8792 --stream --stream-emit-min-interval-seconds 1

# LAN/tailnet dashboard must use a token
mkdir -p ~/.config/kalshibtc
python - <<'PY'
import secrets
from pathlib import Path
path = Path.home() / ".config/kalshibtc/kalshibtc-dashboard.env"
path.write_text(f"KALSHI_BTC15M_DASHBOARD_TOKEN={secrets.token_urlsafe(32)}\n")
path.chmod(0o600)
print(path)
PY
kbtc15 --config configs/live-prod.local.toml dashboard --host 0.0.0.0 --port 8792 --scan-interval-seconds 60 --stream --stream-emit-min-interval-seconds 1
```

The stream page now defaults to a single Execution Decision v1 card: `ACTION` (`BUY_YES`, `BUY_NO`, or `NO_TRADE`), position size, entry, suggested stop, suggested take-profit, confidence, regime, reason, and blockers. The older probability model, EV calculations, YES/NO order book, warnings, raw payload, and prediction reasons are still retained under collapsible debug internals for replay and research. The `/status` page still shows live account balance/portfolio when the selected config has live credentials loaded, paper and live PnL from the local SQLite ledger, latest predictions, live orders/fills, open positions, service status, and the active safety boundary.

The `/status` Paper Trading Performance panel also includes a **Paper PnL Review** table for hypothesis review before any strategy complexity is added. It displays strategy, side, entry/exit times, entry/exit prices, PnL, hold seconds, slope at entry, distance from strike, seconds to expiry, and the signal reason. The same panel includes **Paper Review Buckets** grouped by strategy, side, seconds-to-expiry bucket, distance-from-strike bucket, slope-at-entry bucket, and hold-seconds bucket, each with trades, win rate, average PnL, total PnL, and average hold time. Use these views to answer: does simple slope + above/below actually have edge, and where, or is it just noise?

## Execution Decision v1

The realtime stream now has one deliberately simple operator answer: `execution_decision`. The v1 edge is only:

- BTC above strike + 30s BTC slope above threshold => `BUY_YES`.
- BTC below strike + 30s BTC slope below negative threshold => `BUY_NO`.
- Near strike, flat/missing slope, wrong-way slope, bad market data, wide spread, or final seconds => `NO_TRADE`.

The regime label is only a slope label:

- `uptrend`: 30s BTC slope is positive enough.
- `downtrend`: 30s BTC slope is negative enough.
- `flat_chop`: 30s BTC slope is missing or too small.

Kalshi probability, EV, model probability, order-book details, and prediction reasons remain in the raw/debug payload for replay, but they do **not** create a competing operator-facing action. A countertrend EV can still be logged as `countertrend_ev_watch`; it does not override price relative to strike plus 30s slope.

Default hard blockers are intentionally few: invalid/crossed order book, spread too wide, too close to expiry, missing close clock, missing target, missing 30s slope, and the configured chop zone around the strike. Doji/wick/trap language is treated as later analysis vocabulary, not a v1 regime ontology.

Sizing is risk-capped and fixed at the configured reference size for allowed paper decisions; `NO_TRADE` always has size 0. Stops and take-profit values are simple suggestions in the payload/UI first. Live orders remain guarded and disabled by default; paper-test the one-slope loop before enabling any live path.

To run the dashboard as a user service:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/kalshi-btc15m-dashboard.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kalshi-btc15m-dashboard.service
systemctl --user status kalshi-btc15m-dashboard.service --no-pager
```

The dashboard service template uses `--stream --stream-emit-min-interval-seconds 1`, so `/` is the websocket stream dashboard and `/api/stream` is the latest state JSON. `/status` remains the ledger/account dashboard. The market itself is still a 15-minute Kalshi market using 900-second BTC candles.

## 1s Modular Strategy Core

The `src/kalshibtc/` package is the new small seam for the websocket-first bot. It intentionally mirrors the boring architecture we want long term:

```text
kalshibtc/
  main.py                 # wires MarketStateBuilder + Strategy + RiskManager + Executor
  config.py               # symbols, risk limits, DB paths
  datafeed/               # Tick, OrderBookSnapshot, recorder, websocket protocol
  market/                 # contract, pricing, normalized MarketState
  strategy/               # Signal, Strategy protocol, slope, simple_directional
  execution/              # risk, paper fake fills, disabled Kalshi adapter, position rules
  backtest/               # replay + metrics
  storage/                # SQLite connection + schema.sql
```

The first strategy interface is deliberately plain:

```python
class Strategy:
    def on_tick(self, state) -> Signal:
        ...
```

The first signal object is:

```python
@dataclass(frozen=True)
class Signal:
    side: str          # "long_above", "long_below", "none"
    reason: str
    confidence: float
    strategy: str = "manual"
```

The current `SimpleDirectionalStrategy` does only this:

- `price > strike and slope_30s > 0` → `long_above`.
- `price < strike and slope_30s < 0` → `long_below`.
- otherwise → `none`.

`BotPipeline` can run multiple strategies against the same state, pass each signal through `RiskManager`, and send allowed decisions to `PaperExecutor`. `ReplayEngine` uses the same strategy/risk/paper seams over historical 1s ticks and books, so live paper mode and replay mode exercise the same strategy code. `KalshiBrokerAdapter` is a disabled-by-default future boundary and does not submit orders.

For the live 1s paper research loop, keep databases split:

```text
data/realtime-snapshots-1s.sqlite3       # recorder-owned stream DB; keeps receiving 1s rows
data-live-prod/paper-results-1s.sqlite3  # paper executor results DB; signals/fake fills/exits/PnL
```

Run the paper executor with:

```bash
python -m kalshibtc.paper_signal_executor \
  --snapshot-db data/realtime-snapshots-1s.sqlite3 \
  --results-db data-live-prod/paper-results-1s.sqlite3 \
  --loop --interval-seconds 1
```

The executor does not chmod or make the stream DB read-only; it only SELECTs from it. Generated `predictions` and `paper_trades` are written to the separate results DB so the data tape and hypothesis results cannot overwrite each other.

See [docs/1s-modular-architecture.md](docs/1s-modular-architecture.md) for the module map, flow, safety boundary, and testing commands.

## 1s Websocket Data Feed

`record-1s` is a read-only capture mode for observation sessions:

```bash
kbtc15 --config configs/default.toml record-1s --emit-min-interval-seconds 1
```

It reuses the same websocket state engine as `stream-state`, but attaches a local recorder and writes at most one row per `(market_ticker, second)` to `<data_dir>/realtime-snapshots-1s.sqlite3` in table `realtime_snapshots_1s`. Duplicate websocket events in the same second are upserted so reconnect/re-emission corrections update the latest payload while the feed stays replayable as a normalized 1 Hz tape.

Recorded columns include the operator-decision inputs needed for later replay and labeling:

- Time/contract: timestamp, market ticker, open/close/expiration times, seconds/minutes to close, time bucket.
- BTC/strike state: BTC price, strike/target, signed and absolute distance from strike, distance percent, above/below-strike flag.
- Velocity: 10s/30s/60s BTC velocity, 30s distance velocity, distance expanding flag, recent strike-cross flag, seconds since last cross when known.
- Kalshi confirmation: YES/NO top-of-book, mid prices, spread, top levels/depth, orderbook sequence, crossed/invalid quote warning, market-implied YES.
- Model/debug: probability YES/NO, probability deltas when available, model probability, EV/edge fields, best side, warnings via raw JSON.
- Execution: final `execution_decision` action, side, confidence, regime, reason, and blockers.
- Raw payload: the full stream state JSON for future schema recovery.

Volume/trade velocity is intentionally placeholder-friendly. If the active stream payload exposes cumulative volume or trade events, the recorder stores those fields. If not, it records a `volume_todo` note instead of guessing.

Storage choice for v1: keep the primary recorder in SQLite. It is local, deterministic, zero-network, easy to back up, and avoids adding a remote dependency to a safety-critical observation loop. Supabase is still useful for separate remote feature feeds or later analytics/export, but it is not better than SQLite for the bot's authoritative 1 Hz capture path unless we specifically need multi-machine querying or shared dashboards.

## Guarded demo/live trading

Do not run live mode until paper mode has behaved correctly and you have reviewed the caps in `configs/live-demo.example.toml`.

```bash
# 1) Put credentials outside the repo
mkdir -p ~/.config/kalshibtc
mv kalshi-api-key.key ~/.config/kalshibtc/kalshi-demo.key
chmod 600 ~/.config/kalshibtc/kalshi-demo.key

# 2) Copy the demo-live template to a local untracked config
cp configs/live-demo.example.toml configs/live-demo.local.toml

# 3) Export credential references only; never commit key material
export KALSHI_API_KEY_ID="..."
export KALSHI_PRIVATE_KEY_FILE="$HOME/.config/kalshibtc/kalshi-demo.key"

# 4) Read-only account check; submits no orders
kbtc15 --config configs/live-demo.local.toml auth-check

# 5) Local live ledger/status; submits no orders
kbtc15 --config configs/live-demo.local.toml live-status

# 6) One guarded demo-live scan. If all prediction/risk gates pass, this can submit a tiny IOC limit order.
kbtc15 --config configs/live-demo.local.toml scan

# 7) Continuous guarded demo-live loop. Review logs closely.
kbtc15 --config configs/live-demo.local.toml run --interval-seconds 60
```

Live mode protections:

- RSA-PSS Kalshi request signing using `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_FILE`.
- Private key stays outside the repo and must be readable only by the user.
- Demo acknowledgement: `I_UNDERSTAND_KALSHI_DEMO_ORDERS`.
- Production requires `environment = "production"`, `allow_production = true`, and acknowledgement `I_UNDERSTAND_THIS_SUBMITS_REAL_KALSHI_PRODUCTION_ORDERS`.
- Entries are IOC limit buys only; exits are reduce-only IOC sells.
- Hard caps: max dollars/order, contracts/order, open positions, daily orders, daily loss, min cash reserve, liquidity, spread, and order cooldown.
- Live orders/fills are recorded in `live_orders` and `live_fills`, separate from `paper_trades`.

## Config knobs

- `market_data.provider`: `coinbase` by default; `binance` is available but may be region-limited.
- `predictor.min_edge`: minimum estimated probability edge over Kalshi ask.
- `predictor.min_seconds_to_close`: avoids entering at the final seconds before close.
- `paper.max_position_dollars`: maximum notional per paper trade.
- `paper.kelly_fraction_cap`: caps Kelly-derived sizing.
- `paper.manage_positions`: enables simulated active exits; still paper-only and does not submit orders.
- `paper.take_profit_pct` / `paper.stop_loss_pct`: unrealized PnL thresholds for simulated paper exits.
- `paper.force_close_seconds_to_close`: exits simulated positions near close instead of letting stale marks linger.
- `paper.max_open_trades`, `paper.max_daily_trades`, `paper.max_daily_loss_dollars`: basic opening risk brakes.
- `paper.min_liquidity_dollars`, `paper.max_spread`: public-quote quality gates before opening paper trades.
- `live.environment`: `demo` by default; production requires the stronger production acknowledgement and `allow_production = true`.
- `live.max_order_dollars`, `live.max_contracts`, `live.max_open_positions`, `live.max_daily_orders`, `live.max_daily_loss_dollars`, `live.min_cash_reserve_dollars`: hard live risk brakes.
- `live.take_profit_pct`, `live.stop_loss_pct`, `live.force_close_seconds_to_close`: reduce-only live exit triggers.
- `live.min_seconds_between_orders`: duplicate-order cooldown for both buys and live exits.

## Ledger

SQLite DB split:

- Legacy scanner ledger: `<data_dir>/paper-ledger.sqlite3` (legacy predictions/paper trades plus live audit tables for that config's data directory).
- 1s stream tape: `<data_dir>/realtime-snapshots-1s.sqlite3` (recorder-owned observations; the recorder keeps writing here).
- 1s paper results: `data-live-prod/paper-results-1s.sqlite3` by convention, or any explicit `--results-db` path passed to `python -m kalshibtc.paper_signal_executor`.

The 1s paper executor never stores `predictions` or `paper_trades` in the stream tape; those result tables live in the separate results DB.

Tables:

- `predictions`: every scan decision with probability, edge, model metadata, reasons, and feature snapshot.
- `paper_trades`: simulated YES/NO contract entries, active paper exits, settlement PnL, exit price, and exit reason.
- `live_orders`: guarded live order intents, submitted responses, errors, client IDs, and request JSON.
- `live_fills`: confirmed authenticated fill records used to reconstruct live position state and realized PnL.
- `realtime_snapshots_1s`: normalized recorder-owned websocket observations keyed by `(market_ticker, ts)`, plus raw stream-state JSON for replay. This table lives in the stream DB, not in the 1s results DB.

## Deployment note

A sample paper user-service file is in `deploy/kalshi-btc15m-paper.service`. The continuous 1s paper executor template is in `deploy/kalshi-btc15m-1s-paper.service`; it reads `data/realtime-snapshots-1s.sqlite3` and writes `data-live-prod/paper-results-1s.sqlite3`. A guarded demo-live template is in `deploy/kalshi-btc15m-live-demo.service`; it expects credentials in `%h/.config/kalshibtc/kalshibtc.env` and a local untracked `configs/live-demo.local.toml`. A read-only dashboard template is in `deploy/kalshi-btc15m-dashboard.service`; it expects production env credentials plus `%h/.config/kalshibtc/kalshibtc-dashboard.env` for dashboard auth. None of these services is installed or started automatically. Review every cap before use.

## Verification

Expected local checks:

```bash
pytest
pytest tests/test_kalshibtc_modular_core.py -q
python -m compileall src tests
ruff check .
kbtc15 --config configs/default.toml backtest
kbtc15 --config configs/default.toml run --interval-seconds 1 --max-scans 1
kbtc15 --config configs/default.toml scan
kbtc15 --config configs/default.toml status
kbtc15 --config configs/default.toml report
timeout 5s env KALSHI_BTC15M_DASHBOARD_TOKEN=test-token kbtc15 --config configs/default.toml dashboard --host 127.0.0.1 --port 8792 --stream || test $? -eq 124
kbtc15 --config configs/default.toml live-status
kbtc15 --config configs/default.toml stream-state --help
kbtc15 --config configs/default.toml record-1s --help
python -m kalshibtc.paper_signal_executor --help
kbtc15 --config configs/default.toml stream-paper --help
# With Kalshi WebSocket credentials configured outside the repo:
# kbtc15 --config configs/default.toml stream-state --max-events 3
# kbtc15 --config configs/default.toml record-1s --max-events 3
# kbtc15 --config configs/default.toml stream-paper --max-events 3
# With demo credentials configured outside the repo:
# kbtc15 --config configs/live-demo.local.toml auth-check
```
