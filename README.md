# Kalshi BTC 15m Paper Prediction Bot

A paper-first Python bot for Kalshi's `KXBTC15M` BTC Up/Down 15-minute markets.

Safety boundary: this v1 uses public market data and a local SQLite paper ledger only. It has no code path that submits live Kalshi orders.

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
- Can run continuously with bounded smoke mode, transient provider-error retries, or an unbounded paper-only service command.
- Can try to settle open paper trades by reading Kalshi's public market result after settlement.

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

The default config is `configs/default.toml`. Keep `trading_mode = "paper"` and `enable_live_orders = false`.

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

## Ledger

SQLite ledger path: `data/paper-ledger.sqlite3`

Tables:

- `predictions`: every scan decision with probability, edge, model metadata, reasons, and feature snapshot.
- `paper_trades`: simulated YES/NO contract entries, active paper exits, settlement PnL, exit price, and exit reason.

## Deployment note

A sample user-service file is in `deploy/kalshi-btc15m-paper.service`. It is not installed or started automatically. Review it before use.

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
```
