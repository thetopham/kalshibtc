from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RiskLimits:
    """Small, explicit risk knobs shared by paper/replay/live adapters."""

    base_size_dollars: float = 25.0
    max_position_dollars: float = 25.0
    max_spread: float = 0.05
    min_confidence: float = 0.0
    max_open_positions: int = 1
    cooldown_seconds: float = 0.0

    def clamp_size(self, requested: float) -> float:
        return max(0.0, min(float(requested), float(self.max_position_dollars)))


@dataclass(frozen=True)
class BotConfig:
    """Top-level v2 defaults for the 1-second websocket-first bot."""

    symbol: str = "BTC-USD"
    series_ticker: str = "KXBTC15M"
    data_dir: Path = Path("data")
    snapshot_db_name: str = "realtime-snapshots-1s.sqlite3"
    paper_db_name: str = "paper-ledger.sqlite3"
    risk: RiskLimits = RiskLimits()
    live_orders_enabled: bool = False

    @property
    def snapshot_db_path(self) -> Path:
        return self.data_dir / self.snapshot_db_name

    @property
    def paper_db_path(self) -> Path:
        return self.data_dir / self.paper_db_name
