"""Boring modular 1s Kalshi BTC bot core.

This package is the small v2 seam for websocket data collection, reusable
strategies, backtests, paper execution, and future broker adapters. The older
`kalshi_btc_15m_bot` package remains for the original 1-minute scanner and the
current production CLI while pieces migrate here deliberately.
"""

from .config import BotConfig, RiskLimits

__all__ = ["BotConfig", "RiskLimits"]
