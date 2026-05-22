# Auto Strategy Creator: brownian_edge_high_confidence

Safety boundary: replay/research only; no live orders.

Run ID: `smoke-brownian_edge_high_confidence-20260522T045746Z`
Replay strategy: `strategy_probability_mm_v0`
Metrics: `runs/strategy_probability_mm_v0/smoke-brownian_edge_high_confidence-20260522T045746Z/metrics.json`

## Hypothesis

Executable YES/NO prices sometimes lag a simple distance-to-strike probability; only trade when the edge is unusually large and the probability is away from 50/50.

Mechanism: Brownian probability model with stricter edge, mid-band, and volatility gates.

Falsification rule: Reject if after-fee realized PnL is negative, fills are sparse, or max drawdown exceeds realized PnL on validation/test replay.

## Parameters

```json
{
  "feed_db": "feed/kalshi-btc-1s.sqlite3",
  "from_ts": "2026-05-16T03:34:31+00:00",
  "replay_strategy": "strategy_probability_mm_v0",
  "risk": {
    "base_size_dollars": 25.0,
    "fee_rate": 0.0,
    "fill_timing": "next-tick",
    "max_open_positions": 1,
    "max_position_dollars": 25.0,
    "max_spread": 0.05
  },
  "strategy_params": {
    "base_notional": 10.0,
    "edge_threshold": 0.06,
    "max_atr_slope": 1.2,
    "max_probability_mid_band": 0.08,
    "max_wickiness": 0.7,
    "min_abs_edge": 0.04,
    "min_seconds_to_close": 75.0,
    "min_volatility": 1.0,
    "probability_model": "brownian"
  },
  "to_ts": "2026-05-16T03:45:00+00:00",
  "venue": "kalshi"
}
```

## Result

Snapshots: 554
Signals: 0
Fills: 0
Notional: $0.00
Realized PnL: $0.00
Profit factor: 0.0000
Max drawdown: $0.00
Settlement source: unknown
