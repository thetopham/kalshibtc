from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


@dataclass(frozen=True)
class KalshiConfig:
    base_url: str = "https://external-api.kalshi.com/trade-api/v2"
    series_ticker: str = "KXBTC15M"
    market_status: str = "open"


@dataclass(frozen=True)
class MarketDataConfig:
    provider: str = "coinbase"
    product_id: str = "BTC-USD"
    symbol: str = "BTCUSDT"
    granularity_seconds: int = 900
    lookback_days: int = 21
    request_timeout_seconds: int = 20


@dataclass(frozen=True)
class PredictorConfig:
    min_confidence: float = 0.54
    min_edge: float = 0.035
    min_seconds_to_close: int = 45
    max_seconds_to_close: int = 840
    model_min_train_samples: int = 250
    model_test_fraction: float = 0.25
    rule_weight_floor: float = 0.55


@dataclass(frozen=True)
class PaperConfig:
    initial_cash: float = 1000.0
    auto_trade: bool = True
    max_position_dollars: float = 25.0
    kelly_fraction_cap: float = 0.10
    one_trade_per_market: bool = True
    manage_positions: bool = True
    take_profit_pct: float = 0.30
    stop_loss_pct: float = -0.40
    force_close_seconds_to_close: int = 20
    max_open_trades: int = 2
    max_daily_trades: int = 12
    max_daily_loss_dollars: float = 50.0
    min_liquidity_dollars: float = 50.0
    max_spread: float = 0.20


@dataclass(frozen=True)
class BacktestConfig:
    confidence_threshold: float = 0.56
    synthetic_entry_price: float = 0.53


@dataclass(frozen=True)
class BotConfig:
    trading_mode: str = "paper"
    enable_live_orders: bool = False
    data_dir: Path = Path("data")
    kalshi: KalshiConfig = KalshiConfig()
    market_data: MarketDataConfig = MarketDataConfig()
    predictor: PredictorConfig = PredictorConfig()
    paper: PaperConfig = PaperConfig()
    backtest: BacktestConfig = BacktestConfig()

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "paper-ledger.sqlite3"


def _deep_get(raw: dict[str, Any], section: str, key: str, default: Any) -> Any:
    value = raw.get(section, {})
    if not isinstance(value, dict):
        return default
    return value.get(key, default)


def _resolve_data_dir(value: str | Path) -> Path:
    override = os.getenv("KALSHI_BTC15M_DATA_DIR")
    path = Path(override or value)
    return path.expanduser().resolve() if path.is_absolute() else (Path.cwd() / path).resolve()


def load_config(path: str | Path | None = None) -> BotConfig:
    """Load config and enforce paper-only safety defaults.

    Live order submission is intentionally unsupported in v1. If a config tries to
    enable it, fail closed before any network trading code can run.
    """
    load_dotenv()
    config_path = Path(path or os.getenv("KALSHI_BTC15M_CONFIG", "configs/default.toml"))
    raw: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("rb") as fh:
            raw = tomllib.load(fh)

    cfg = BotConfig(
        trading_mode=str(raw.get("trading_mode", "paper")),
        enable_live_orders=bool(raw.get("enable_live_orders", False)),
        data_dir=_resolve_data_dir(raw.get("data_dir", "data")),
        kalshi=KalshiConfig(
            base_url=str(_deep_get(raw, "kalshi", "base_url", KalshiConfig.base_url)),
            series_ticker=str(_deep_get(raw, "kalshi", "series_ticker", KalshiConfig.series_ticker)),
            market_status=str(_deep_get(raw, "kalshi", "market_status", KalshiConfig.market_status)),
        ),
        market_data=MarketDataConfig(
            provider=str(_deep_get(raw, "market_data", "provider", MarketDataConfig.provider)),
            product_id=str(_deep_get(raw, "market_data", "product_id", MarketDataConfig.product_id)),
            symbol=str(_deep_get(raw, "market_data", "symbol", MarketDataConfig.symbol)),
            granularity_seconds=int(
                _deep_get(raw, "market_data", "granularity_seconds", MarketDataConfig.granularity_seconds)
            ),
            lookback_days=int(_deep_get(raw, "market_data", "lookback_days", MarketDataConfig.lookback_days)),
            request_timeout_seconds=int(
                _deep_get(raw, "market_data", "request_timeout_seconds", MarketDataConfig.request_timeout_seconds)
            ),
        ),
        predictor=PredictorConfig(
            min_confidence=float(_deep_get(raw, "predictor", "min_confidence", PredictorConfig.min_confidence)),
            min_edge=float(_deep_get(raw, "predictor", "min_edge", PredictorConfig.min_edge)),
            min_seconds_to_close=int(
                _deep_get(raw, "predictor", "min_seconds_to_close", PredictorConfig.min_seconds_to_close)
            ),
            max_seconds_to_close=int(
                _deep_get(raw, "predictor", "max_seconds_to_close", PredictorConfig.max_seconds_to_close)
            ),
            model_min_train_samples=int(
                _deep_get(raw, "predictor", "model_min_train_samples", PredictorConfig.model_min_train_samples)
            ),
            model_test_fraction=float(
                _deep_get(raw, "predictor", "model_test_fraction", PredictorConfig.model_test_fraction)
            ),
            rule_weight_floor=float(
                _deep_get(raw, "predictor", "rule_weight_floor", PredictorConfig.rule_weight_floor)
            ),
        ),
        paper=PaperConfig(
            initial_cash=float(_deep_get(raw, "paper", "initial_cash", PaperConfig.initial_cash)),
            auto_trade=bool(_deep_get(raw, "paper", "auto_trade", PaperConfig.auto_trade)),
            max_position_dollars=float(
                _deep_get(raw, "paper", "max_position_dollars", PaperConfig.max_position_dollars)
            ),
            kelly_fraction_cap=float(
                _deep_get(raw, "paper", "kelly_fraction_cap", PaperConfig.kelly_fraction_cap)
            ),
            one_trade_per_market=bool(
                _deep_get(raw, "paper", "one_trade_per_market", PaperConfig.one_trade_per_market)
            ),
            manage_positions=bool(_deep_get(raw, "paper", "manage_positions", PaperConfig.manage_positions)),
            take_profit_pct=float(_deep_get(raw, "paper", "take_profit_pct", PaperConfig.take_profit_pct)),
            stop_loss_pct=float(_deep_get(raw, "paper", "stop_loss_pct", PaperConfig.stop_loss_pct)),
            force_close_seconds_to_close=int(
                _deep_get(
                    raw,
                    "paper",
                    "force_close_seconds_to_close",
                    PaperConfig.force_close_seconds_to_close,
                )
            ),
            max_open_trades=int(_deep_get(raw, "paper", "max_open_trades", PaperConfig.max_open_trades)),
            max_daily_trades=int(
                _deep_get(raw, "paper", "max_daily_trades", PaperConfig.max_daily_trades)
            ),
            max_daily_loss_dollars=float(
                _deep_get(raw, "paper", "max_daily_loss_dollars", PaperConfig.max_daily_loss_dollars)
            ),
            min_liquidity_dollars=float(
                _deep_get(raw, "paper", "min_liquidity_dollars", PaperConfig.min_liquidity_dollars)
            ),
            max_spread=float(_deep_get(raw, "paper", "max_spread", PaperConfig.max_spread)),
        ),
        backtest=BacktestConfig(
            confidence_threshold=float(
                _deep_get(raw, "backtest", "confidence_threshold", BacktestConfig.confidence_threshold)
            ),
            synthetic_entry_price=float(
                _deep_get(raw, "backtest", "synthetic_entry_price", BacktestConfig.synthetic_entry_price)
            ),
        ),
    )

    if cfg.trading_mode != "paper":
        raise ValueError("Refusing to start: v1 supports trading_mode='paper' only.")
    if cfg.enable_live_orders:
        raise ValueError("Refusing to start: enable_live_orders must remain false in v1.")
    if cfg.market_data.granularity_seconds != 900:
        raise ValueError("This bot is intentionally scoped to 15-minute BTC markets (900-second candles).")

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
