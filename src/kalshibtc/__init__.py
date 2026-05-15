"""Boring modular 1s Kalshi BTC paper bot core.

Archived legacy code lives under ``archive/legacy-15m`` and is not part of the
active Python package path. New 1s strategy, risk, storage, replay, and paper
execution work belongs here.
"""

from .config import BotConfig, RiskLimits

__all__ = ["BotConfig", "RiskLimits"]
