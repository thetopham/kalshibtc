from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from kalshi_btc_15m_bot.features import make_supervised_dataset


def sample_frame(n: int = 80) -> pd.DataFrame:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    price = 100.0
    for i in range(n):
        open_price = price
        close = open_price + (1 if i % 3 == 0 else -0.5)
        high = max(open_price, close) + 0.2
        low = min(open_price, close) - 0.2
        rows.append(
            {
                "date": start + timedelta(minutes=15 * i),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": 10 + i,
            }
        )
        price = close
    return pd.DataFrame(rows).set_index("date")


def test_supervised_target_uses_next_candle_direction() -> None:
    df = sample_frame()
    X, y = make_supervised_dataset(df)
    ts = X.index[10]
    next_pos = df.index.get_loc(ts) + 1
    expected = int(df.iloc[next_pos]["close"] >= df.iloc[next_pos]["open"])
    assert int(y.loc[ts]) == expected


def test_dataset_has_no_nan_or_inf_features() -> None:
    X, y = make_supervised_dataset(sample_frame())
    assert len(X) == len(y)
    assert np.isfinite(X.to_numpy()).all()
