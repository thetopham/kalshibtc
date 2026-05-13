from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from .features import latest_features
from .models import KalshiMarket


@dataclass(frozen=True)
class RuleSignal:
    probability_yes: float
    confidence: float
    reasons: list[str]
    feature_snapshot: dict[str, float]


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, x))))


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def rule_based_probability(
    df: pd.DataFrame,
    *,
    market: KalshiMarket,
    current_price: float,
    now: datetime,
) -> RuleSignal:
    """Transparent strategy blend from the source thread.

    It uses the classic ingredients from the thread (momentum, SMA trend, mean
    reversion) plus the Kalshi-specific fact that the target is the start-of-window
    BTC benchmark and the market expires very soon.
    """
    row = latest_features(df)
    snap = {k: float(row[k]) for k in row.index if isinstance(row[k], (int, float))}
    reasons: list[str] = []
    logit = 0.0

    momentum_4 = float(row["momentum_4"])
    momentum_8 = float(row["momentum_8"])
    sma_fast = float(row["sma_gap_4_16"])
    sma_slow = float(row["sma_gap_8_32"])
    zscore_16 = float(row["zscore_16"])
    zscore_32 = float(row["zscore_32"])
    volume_z = float(row["volume_z_16"])
    vol_16 = max(abs(float(row["vol_16"])), 0.0005)

    momentum_score = clamp((momentum_4 / vol_16) * 0.35 + (momentum_8 / vol_16) * 0.20, -2, 2)
    logit += momentum_score
    reasons.append(f"momentum_score={momentum_score:+.2f} from 1h/2h log returns")

    trend_score = clamp((sma_fast / vol_16) * 0.25 + (sma_slow / vol_16) * 0.15, -1.5, 1.5)
    logit += trend_score
    reasons.append(f"sma_trend_score={trend_score:+.2f} from 4/16 and 8/32 candle SMA gaps")

    # Short-horizon BTC often mean-reverts after stretched moves, so z-score is
    # used as a counter-trend brake rather than a trend confirmation.
    mean_reversion_score = clamp(-(zscore_16 * 0.25 + zscore_32 * 0.10), -1.2, 1.2)
    logit += mean_reversion_score
    reasons.append(f"mean_reversion_score={mean_reversion_score:+.2f} from rolling z-scores")

    if market.target_price and math.isfinite(market.target_price) and market.target_price > 0:
        distance = (current_price - market.target_price) / market.target_price
        seconds_left = market.seconds_to_close(now)
        time_factor = 1.0
        if seconds_left is not None:
            # The closer we are to expiry, the more distance-to-target matters.
            time_factor = clamp(900.0 / max(seconds_left, 30.0), 0.75, 5.0)
        target_score = clamp((distance / vol_16) * 0.55 * time_factor, -4.0, 4.0)
        logit += target_score
        reasons.append(
            f"target_distance_score={target_score:+.2f}; current={current_price:.2f}, target={market.target_price:.2f}"
        )
        snap["target_distance_pct"] = distance
        snap["seconds_to_close"] = seconds_left if seconds_left is not None else math.nan
    else:
        reasons.append("missing_target_price: skipped Kalshi distance-to-target term")

    # Volume confirms trend but should not dominate a 15-minute expiry signal.
    if abs(volume_z) > 1.0:
        volume_score = clamp(math.copysign(min(abs(volume_z), 3.0) * 0.10, momentum_score), -0.3, 0.3)
        logit += volume_score
        reasons.append(f"volume_confirmation={volume_score:+.2f} (volume_z={volume_z:+.2f})")

    probability_yes = clamp(sigmoid(logit), 0.02, 0.98)
    confidence = abs(probability_yes - 0.5) * 2.0
    reasons.append(f"rule_probability_yes={probability_yes:.3f}")
    return RuleSignal(probability_yes=probability_yes, confidence=confidence, reasons=reasons, feature_snapshot=snap)
