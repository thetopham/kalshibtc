# Source notes

Primary trading-series ideas used from the supplied @sopersone links:

1. Data -> analysis -> signal -> trade.
2. Use Python, an isolated environment, and reproducible project structure.
3. Fetch OHLCV candles, clean them, calculate returns, and store local state.
4. Implement numerical strategies: SMA crossover, momentum, mean reversion.
5. Shift features/positions forward to avoid look-ahead bias.
6. Evaluate with backtests and risk metrics before live use.
7. Prefer event-driven accounting for realistic state, costs, and fills.
8. Treat classical ML as modest edge detection, not magic.

Kalshi-specific findings:

- Series ticker for BTC 15-minute up/down markets: `KXBTC15M`.
- Public market endpoint: `GET https://external-api.kalshi.com/trade-api/v2/markets?series_ticker=KXBTC15M&status=open`.
- Current markets expose target/start price as `floor_strike` and current YES/NO bid/ask fields.
- Rules state the market resolves YES if the final 60-second average of CF Benchmarks BRTI is at least the starting 60-second average.
- Coinbase/Binance are proxies only; they are not the settlement source.

2026-05 observation-session architecture update:

- The main development target is now the 1-second websocket bot, not more feature work in the original 1-minute scanner.
- Keep collecting replayable 1 Hz BTC + Kalshi order-book state before adding clever strategies.
- Treat BTC price, strike distance, time remaining, and slope/velocity as primary state. Kalshi probability/order-book/volume are confirmation and crowd repricing, not sole price discovery.
- Useful contract archetypes for later labeling: immediate `trend_hold`, `indecision_doji`, and `wick_reversal_risk` / late reversal traps.
- Separate signal generation from execution immediately: a `Strategy.on_tick(state) -> Signal` must run unchanged in replay, live paper mode, and future guarded real-order mode.
- New boring modules live under `src/kalshibtc/`; legacy scanner/guardrails remain in `src/kalshi_btc_15m_bot/` until migrated deliberately.
