from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .config import BotConfig
from .features import make_supervised_dataset
from .market_data import candles_to_frame, provider_from_config


@dataclass(frozen=True)
class BacktestResult:
    metrics: dict[str, Any]
    trades: list[dict[str, Any]]


def run_directional_backtest(df: pd.DataFrame, config: BotConfig) -> BacktestResult:
    """Offline walk-forward-ish directional backtest.

    This evaluates the BTC direction model on historical 15m candles. It does not
    use historical Kalshi order books, so the PnL is a synthetic binary-contract
    sanity check rather than a claim about real fills.
    """
    X, y = make_supervised_dataset(df)
    if len(X) < config.predictor.model_min_train_samples:
        raise ValueError(f"Not enough samples for backtest: {len(X)}")

    split = int(len(X) * 0.65)
    test_size = len(X) - split
    if test_size < 50:
        raise ValueError("Need at least 50 test samples")

    model = Pipeline(
        [
            ("scaler", StandardScaler()),
            ("classifier", LogisticRegression(max_iter=1000, class_weight="balanced")),
        ]
    )
    model.fit(X.iloc[:split], y.iloc[:split])
    probabilities = model.predict_proba(X.iloc[split:])[:, 1]
    actual = y.iloc[split:].to_numpy()
    predicted = (probabilities >= 0.5).astype(int)
    accuracy = float(accuracy_score(actual, predicted))
    brier = float(brier_score_loss(actual, probabilities))

    threshold = config.backtest.confidence_threshold
    entry_price = config.backtest.synthetic_entry_price
    trades: list[dict[str, Any]] = []
    pnl = 0.0
    wins = 0
    for ts, prob, outcome in zip(X.index[split:], probabilities, actual, strict=True):
        side: str | None = None
        side_probability = 0.0
        if prob >= threshold:
            side = "YES"
            side_probability = float(prob)
            correct = outcome == 1
        elif prob <= 1.0 - threshold:
            side = "NO"
            side_probability = float(1.0 - prob)
            correct = outcome == 0
        else:
            continue

        # Spend $1 notional at synthetic ask. A correct binary contract returns
        # one dollar per contract; an incorrect one expires worthless.
        contracts = 1.0 / entry_price
        trade_pnl = (contracts if correct else 0.0) - 1.0
        pnl += trade_pnl
        wins += int(correct)
        trades.append(
            {
                "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "side": side,
                "probability": side_probability,
                "actual_up": int(outcome),
                "correct": bool(correct),
                "pnl": trade_pnl,
            }
        )

    trade_count = len(trades)
    win_rate = wins / trade_count if trade_count else 0.0
    avg_pnl = pnl / trade_count if trade_count else 0.0
    baseline_up_rate = float(np.mean(actual))
    metrics = {
        "samples": int(len(X)),
        "train_samples": int(split),
        "test_samples": int(test_size),
        "test_accuracy": accuracy,
        "test_brier": brier,
        "baseline_up_rate": baseline_up_rate,
        "trade_count": trade_count,
        "trade_win_rate": win_rate,
        "synthetic_entry_price": entry_price,
        "synthetic_total_pnl_per_1usd_trades": pnl,
        "synthetic_avg_pnl_per_trade": avg_pnl,
        "threshold": threshold,
        "note": "Synthetic PnL assumes a constant binary ask; it is for model sanity only, not real Kalshi fills.",
    }
    return BacktestResult(metrics=metrics, trades=trades)


def run_backtest_from_provider(config: BotConfig) -> BacktestResult:
    provider = provider_from_config(config.market_data)
    candles = provider.fetch_candles(config.market_data.lookback_days)
    frame = candles_to_frame(
        candles,
        granularity_seconds=config.market_data.granularity_seconds,
        drop_incomplete=True,
    )
    return run_directional_backtest(frame, config)
