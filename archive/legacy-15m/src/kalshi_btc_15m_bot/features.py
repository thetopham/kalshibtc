from __future__ import annotations

import math

import numpy as np
import pandas as pd

FEATURE_COLUMNS = [
    "ret_1",
    "ret_2",
    "ret_3",
    "ret_4",
    "ret_8",
    "ret_16",
    "vol_8",
    "vol_16",
    "sma_gap_4_16",
    "sma_gap_8_32",
    "momentum_4",
    "momentum_8",
    "zscore_16",
    "zscore_32",
    "volume_z_16",
    "range_pct",
    "close_position",
]


def add_returns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["simple_return"] = out["close"].pct_change()
    out["log_return"] = np.log(out["close"] / out["close"].shift(1))
    return out


def add_technical_features(df: pd.DataFrame) -> pd.DataFrame:
    out = add_returns(df)

    for lag in [1, 2, 3, 4, 8, 16]:
        out[f"ret_{lag}"] = np.log(out["close"] / out["close"].shift(lag))

    out["vol_8"] = out["log_return"].rolling(8).std()
    out["vol_16"] = out["log_return"].rolling(16).std()

    sma_4 = out["close"].rolling(4).mean()
    sma_16 = out["close"].rolling(16).mean()
    sma_8 = out["close"].rolling(8).mean()
    sma_32 = out["close"].rolling(32).mean()
    out["sma_gap_4_16"] = (sma_4 / sma_16) - 1.0
    out["sma_gap_8_32"] = (sma_8 / sma_32) - 1.0

    out["momentum_4"] = np.log(out["close"] / out["close"].shift(4))
    out["momentum_8"] = np.log(out["close"] / out["close"].shift(8))

    mean_16 = out["close"].rolling(16).mean()
    std_16 = out["close"].rolling(16).std()
    mean_32 = out["close"].rolling(32).mean()
    std_32 = out["close"].rolling(32).std()
    out["zscore_16"] = (out["close"] - mean_16) / std_16.replace(0, np.nan)
    out["zscore_32"] = (out["close"] - mean_32) / std_32.replace(0, np.nan)

    volume_mean = out["volume"].rolling(16).mean()
    volume_std = out["volume"].rolling(16).std()
    out["volume_z_16"] = (out["volume"] - volume_mean) / volume_std.replace(0, np.nan)

    out["range_pct"] = (out["high"] - out["low"]) / out["close"]
    candle_range = (out["high"] - out["low"]).replace(0, np.nan)
    out["close_position"] = ((out["close"] - out["low"]) / candle_range).clip(0, 1)

    return out


def make_supervised_dataset(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Build bar-start labels without look-ahead leakage.

    Features at candle t are used to predict whether candle t+1 closes at or
    above its own open, which approximates Kalshi's 15m up/down contract target.
    """
    feat = add_technical_features(df)
    next_close = df["close"].shift(-1)
    next_open = df["open"].shift(-1)
    feat["target_up"] = np.where(
        next_close.notna() & next_open.notna(),
        (next_close >= next_open).astype(int),
        np.nan,
    )
    dataset = feat[FEATURE_COLUMNS + ["target_up"]].replace([np.inf, -np.inf], np.nan).dropna()
    X = dataset[FEATURE_COLUMNS]
    y = dataset["target_up"].astype(int)
    return X, y


def latest_features(df: pd.DataFrame) -> pd.Series:
    feat = add_technical_features(df).replace([np.inf, -np.inf], np.nan)
    valid = feat.dropna(subset=FEATURE_COLUMNS)
    if valid.empty:
        raise ValueError("Not enough candles to compute technical features")
    return valid.iloc[-1]


def feature_snapshot(row: pd.Series) -> dict[str, float]:
    snap: dict[str, float] = {}
    for col in FEATURE_COLUMNS:
        value = float(row[col])
        if math.isfinite(value):
            snap[col] = value
    return snap
