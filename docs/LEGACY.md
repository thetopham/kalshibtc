# Legacy Package

Legacy path: `src/kalshi_btc_15m_bot/`.

This package is the older bot line. It remains in the repo for reference and compatibility, but it is not the place for new strategy work while the current 1s bot is being evaluated.

## What it is

`src/kalshi_btc_15m_bot/` contains the original 1-minute scanner and accumulated operating surface:

- `cli.py` exposes the historical `kbtc15` commands.
- `bot.py`, `predictor.py`, `features.py`, `strategies.py`, `market_data.py`, and `backtest.py` implement the older scan/run/model-blend path.
- `ledger.py`, `paper_performance.py`, and `dashboard.py` support the old ledger, reports, and dashboard views.
- `streaming.py` and `recorder.py` include the websocket stream/recording code used by `stream-state`, `stream-paper`, and `record-1s`.
- `live.py` and `kalshi_client.py` contain guarded live-order and Kalshi API plumbing.
- `config.py`, `models.py`, `doctor.py`, and `supabase_features.py` support configuration, shared models, diagnostics, and optional feature plumbing.

## Why it exists

The legacy package is useful reference code. It has working CLI commands, Kalshi API/signing knowledge, websocket handling, SQLite ledgers, dashboard/report patterns, and safety guardrails that may be reused later.

It also accumulated feature creep: 1-minute scanning, predictor/model blending, dashboard research views, ledger/account status, guarded live paths, and multiple decision vocabularies. That makes it harder to reason about the current 1s strategy question.

## What can be reused later

Reuse ideas or code only after the 1s paper loop has evidence and a specific need:

- Kalshi API client and RSA-PSS signing patterns.
- Guarded live-order caps and acknowledgement checks.
- Dashboard/report display patterns.
- SQLite ledger/audit patterns.
- Websocket reconnect and recorder lessons.
- Public market metadata helpers.

When reusing anything, pull it across a clear adapter seam. Do not merge the packages wholesale.

## What not to modify during current 1s work

During the current boring 1s bot phase:

- Do not add new strategy logic to `src/kalshi_btc_15m_bot/`.
- Do not expand the predictor/model blend.
- Do not add ML, market making, EV blending, or new indicators.
- Do not enable or loosen live-order behavior.
- Do not refactor legacy code into the new package just to make it feel tidy.
- Do not delete legacy code.
- Do not merge the legacy and 1s implementations.

New strategy/risk/execution/replay work belongs in `src/kalshibtc/`.

## Current relationship to the 1s bot

The current 1s data collector is still launched through the `kbtc15 record-1s` command, which lives under the legacy CLI surface. That is acceptable for now.

The current strategy/risk/execution loop is `python -m kalshibtc.paper_signal_executor`, and new modular work should stay under `src/kalshibtc/`.
