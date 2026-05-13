# Kalshi BTC 15m Bot

A paper-first Python bot for Kalshi's `KXBTC15M` BTC Up/Down 15-minute markets, with an explicit guarded live adapter for tiny IOC limit orders once you opt in.

Safety boundary: default config is paper-only. Live trading requires a separate live config, Kalshi credentials outside the repo, a literal acknowledgement string, demo/production environment selection, hard dollar/contract caps, cash reserve gates, and SQLite audit logging before any order path is constructed.

## What it does

- Finds the current Kalshi BTC 15-minute up/down market from the public API.
- Pulls BTC 15-minute candles from Coinbase by default, with optional Binance REST support.
- Builds features from the source series: returns, SMA trend, momentum, mean reversion z-scores, volatility, and volume.
- Trains a modest logistic-regression direction model on recent 15-minute BTC candles using time-ordered train/test splits.
- Blends the ML probability with transparent rule-based logic and the live distance to the Kalshi target price.
- Compares predicted probability to Kalshi YES/NO asks and records a local paper trade only when edge gates pass.
- Stores predictions and paper trades in `data/paper-ledger.sqlite3`.
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

# Bounded loop for smoke testing service behavior
kbtc15 --config configs/default.toml run --interval-seconds 1 --max-scans 2

# JSON output for automation
kbtc15 --config configs/default.toml --json scan

# Local paper ledger status with open-position mark-to-market when Kalshi public quotes are reachable
kbtc15 --config configs/default.toml status

# Operator report with equity, PnL, win rate, expectancy, and open-position exit signals
kbtc15 --config configs/default.toml report

# Try settling open paper trades from public Kalshi results
kbtc15 --config configs/default.toml resolve

# View current KXBTC15M market metadata
kbtc15 --config configs/default.toml markets

# Offline directional backtest on recent 15m BTC candles
kbtc15 --config configs/default.toml backtest
```

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

SQLite ledger path: `<data_dir>/paper-ledger.sqlite3` (the live audit tables live in the same SQLite file for that config's data directory).

Tables:

- `predictions`: every scan decision with probability, edge, model metadata, reasons, and feature snapshot.
- `paper_trades`: simulated YES/NO contract entries, active paper exits, settlement PnL, exit price, and exit reason.
- `live_orders`: guarded live order intents, submitted responses, errors, client IDs, and request JSON.
- `live_fills`: confirmed authenticated fill records used to reconstruct live position state and realized PnL.

## Deployment note

A sample paper user-service file is in `deploy/kalshi-btc15m-paper.service`. A guarded demo-live template is in `deploy/kalshi-btc15m-live-demo.service`; it expects credentials in `%h/.config/kalshibtc/kalshibtc.env` and a local untracked `configs/live-demo.local.toml`. Neither service is installed or started automatically. Review every cap before use.

## Verification

Expected local checks:

```bash
pytest
python -m compileall src tests
ruff check .
kbtc15 --config configs/default.toml backtest
kbtc15 --config configs/default.toml run --interval-seconds 1 --max-scans 1
kbtc15 --config configs/default.toml scan
kbtc15 --config configs/default.toml status
kbtc15 --config configs/default.toml report
kbtc15 --config configs/default.toml live-status
# With demo credentials configured outside the repo:
# kbtc15 --config configs/live-demo.local.toml auth-check
```
