from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
class SupabaseFeatureConfig:
    enabled: bool = False
    url: str = ""
    api_key: str = ""
    table: str = "tv_datafeed_btc"
    symbol: str = "BTCUSD"
    timeframe: int = 1
    max_feature_age_seconds: float = 90.0
    request_timeout_seconds: float = 10.0


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
class LiveConfig:
    environment: str = "demo"
    base_url: str = "https://external-api.demo.kalshi.co/trade-api/v2"
    acknowledgement: str = ""
    auto_trade: bool = False
    max_order_dollars: float = 0.0
    max_contracts: int = 0
    max_open_positions: int = 1
    max_daily_orders: int = 0
    max_daily_loss_dollars: float = 0.0
    min_cash_reserve_dollars: float = 0.0
    min_liquidity_dollars: float = 50.0
    max_spread: float = 0.10
    take_profit_pct: float = 0.30
    stop_loss_pct: float = -0.40
    force_close_seconds_to_close: int = 20
    min_seconds_between_orders: int = 60
    time_in_force: str = "immediate_or_cancel"
    subaccount: int = 0
    allow_production: bool = False


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
    supabase_features: SupabaseFeatureConfig = SupabaseFeatureConfig()
    predictor: PredictorConfig = PredictorConfig()
    paper: PaperConfig = PaperConfig()
    live: LiveConfig = LiveConfig()
    backtest: BacktestConfig = BacktestConfig()

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "paper-ledger.sqlite3"

    @property
    def paper_results_1s_path(self) -> Path:
        return self.data_dir / "paper-results-1s.sqlite3"

    @property
    def realtime_snapshots_path(self) -> Path:
        return self.data_dir / "realtime-snapshots-1s.sqlite3"

    @property
    def is_live_mode(self) -> bool:
        return self.trading_mode == "live" and self.enable_live_orders


def _deep_get(raw: dict[str, Any], section: str, key: str, default: Any) -> Any:
    value = raw.get(section, {})
    if not isinstance(value, dict):
        return default
    return value.get(key, default)


def _resolve_data_dir(value: str | Path, *, allow_env_override: bool) -> Path:
    # Production/demo live configs intentionally set their own data_dir so live
    # fills cannot be mixed with a stale paper .env override. Keep the env
    # override only for configs that omit data_dir entirely.
    override = os.getenv("KALSHI_BTC15M_DATA_DIR") if allow_env_override else None
    path = Path(override or value)
    return path.expanduser().resolve() if path.is_absolute() else (Path.cwd() / path).resolve()


def load_config(path: str | Path | None = None) -> BotConfig:
    """Load config with fail-closed paper/live trading boundaries."""
    config_path = Path(path or os.getenv("KALSHI_BTC15M_CONFIG", "configs/default.toml"))
    _load_config_dotenv(config_path)
    raw: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("rb") as fh:
            raw = tomllib.load(fh)

    live_environment = str(_deep_get(raw, "live", "environment", LiveConfig.environment)).lower()
    live_base_default = (
        "https://external-api.demo.kalshi.co/trade-api/v2"
        if live_environment == "demo"
        else KalshiConfig.base_url
    )

    cfg = BotConfig(
        trading_mode=str(raw.get("trading_mode", "paper")),
        enable_live_orders=bool(raw.get("enable_live_orders", False)),
        data_dir=_resolve_data_dir(raw.get("data_dir", "data"), allow_env_override="data_dir" not in raw),
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
        supabase_features=SupabaseFeatureConfig(
            enabled=bool(_deep_get(raw, "supabase_features", "enabled", SupabaseFeatureConfig.enabled)),
            url=str(
                _deep_get(
                    raw,
                    "supabase_features",
                    "url",
                    os.getenv("KALSHI_BTC15M_SUPABASE_URL") or os.getenv("SUPABASE_URL") or "",
                )
            ),
            api_key=str(
                _deep_get(
                    raw,
                    "supabase_features",
                    "api_key",
                    os.getenv("KALSHI_BTC15M_SUPABASE_API_KEY")
                    or os.getenv("SUPABASE_ANON_KEY")
                    or "",
                )
            ),
            table=str(_deep_get(raw, "supabase_features", "table", SupabaseFeatureConfig.table)),
            symbol=str(_deep_get(raw, "supabase_features", "symbol", SupabaseFeatureConfig.symbol)),
            timeframe=int(_deep_get(raw, "supabase_features", "timeframe", SupabaseFeatureConfig.timeframe)),
            max_feature_age_seconds=float(
                _deep_get(
                    raw,
                    "supabase_features",
                    "max_feature_age_seconds",
                    SupabaseFeatureConfig.max_feature_age_seconds,
                )
            ),
            request_timeout_seconds=float(
                _deep_get(
                    raw,
                    "supabase_features",
                    "request_timeout_seconds",
                    SupabaseFeatureConfig.request_timeout_seconds,
                )
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
        live=LiveConfig(
            environment=live_environment,
            base_url=str(_deep_get(raw, "live", "base_url", live_base_default)),
            acknowledgement=str(_deep_get(raw, "live", "acknowledgement", LiveConfig.acknowledgement)),
            auto_trade=bool(_deep_get(raw, "live", "auto_trade", LiveConfig.auto_trade)),
            max_order_dollars=float(_deep_get(raw, "live", "max_order_dollars", LiveConfig.max_order_dollars)),
            max_contracts=int(_deep_get(raw, "live", "max_contracts", LiveConfig.max_contracts)),
            max_open_positions=int(
                _deep_get(raw, "live", "max_open_positions", LiveConfig.max_open_positions)
            ),
            max_daily_orders=int(_deep_get(raw, "live", "max_daily_orders", LiveConfig.max_daily_orders)),
            max_daily_loss_dollars=float(
                _deep_get(raw, "live", "max_daily_loss_dollars", LiveConfig.max_daily_loss_dollars)
            ),
            min_cash_reserve_dollars=float(
                _deep_get(raw, "live", "min_cash_reserve_dollars", LiveConfig.min_cash_reserve_dollars)
            ),
            min_liquidity_dollars=float(
                _deep_get(raw, "live", "min_liquidity_dollars", LiveConfig.min_liquidity_dollars)
            ),
            max_spread=float(_deep_get(raw, "live", "max_spread", LiveConfig.max_spread)),
            take_profit_pct=float(_deep_get(raw, "live", "take_profit_pct", LiveConfig.take_profit_pct)),
            stop_loss_pct=float(_deep_get(raw, "live", "stop_loss_pct", LiveConfig.stop_loss_pct)),
            force_close_seconds_to_close=int(
                _deep_get(
                    raw,
                    "live",
                    "force_close_seconds_to_close",
                    LiveConfig.force_close_seconds_to_close,
                )
            ),
            min_seconds_between_orders=int(
                _deep_get(raw, "live", "min_seconds_between_orders", LiveConfig.min_seconds_between_orders)
            ),
            time_in_force=str(_deep_get(raw, "live", "time_in_force", LiveConfig.time_in_force)),
            subaccount=int(_deep_get(raw, "live", "subaccount", LiveConfig.subaccount)),
            allow_production=bool(_deep_get(raw, "live", "allow_production", LiveConfig.allow_production)),
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

    _validate_config(cfg)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _load_config_dotenv(config_path: Path) -> None:
    """Load only the operator's local .env files, never a searched parent repo .env.

    python-dotenv's default search can leak a developer checkout .env into tests or
    other working directories. Keep loading deterministic: current directory first,
    then the directory containing an explicit config path.
    """
    candidates = [Path.cwd() / ".env"]
    config_dir = config_path.expanduser().parent
    if config_dir != Path("."):
        config_env = config_dir if config_dir.is_absolute() else Path.cwd() / config_dir
        candidates.append(config_env / ".env")
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            load_dotenv(dotenv_path=resolved, override=False)


def _validate_config(cfg: BotConfig) -> None:
    if cfg.trading_mode not in {"paper", "live"}:
        raise ValueError("trading_mode must be either 'paper' or 'live'.")
    if cfg.enable_live_orders and cfg.trading_mode != "live":
        raise ValueError("enable_live_orders=true requires trading_mode='live'.")
    if cfg.trading_mode == "live" and not cfg.enable_live_orders:
        raise ValueError("trading_mode='live' requires enable_live_orders=true.")
    if cfg.market_data.granularity_seconds != 900:
        raise ValueError("This bot is intentionally scoped to 15-minute BTC markets (900-second candles).")
    if cfg.supabase_features.enabled:
        _validate_supabase_feature_config(cfg.supabase_features)
    if cfg.is_live_mode:
        _validate_live_config(cfg.live)


def _validate_supabase_feature_config(config: SupabaseFeatureConfig) -> None:
    if not config.url.startswith("https://"):
        raise ValueError("supabase_features.url must be an HTTPS Supabase project URL when enabled.")
    if not config.api_key:
        raise ValueError("supabase_features.api_key is required when supabase_features.enabled=true.")
    if not config.table:
        raise ValueError("supabase_features.table is required when supabase_features.enabled=true.")
    if not config.symbol:
        raise ValueError("supabase_features.symbol is required when supabase_features.enabled=true.")
    if config.timeframe < 1:
        raise ValueError("supabase_features.timeframe must be at least 1 minute.")
    if config.max_feature_age_seconds <= 0:
        raise ValueError("supabase_features.max_feature_age_seconds must be greater than zero.")
    if config.request_timeout_seconds <= 0:
        raise ValueError("supabase_features.request_timeout_seconds must be greater than zero.")


def _validate_live_config(live: LiveConfig) -> None:
    if live.environment not in {"demo", "production"}:
        raise ValueError("live.environment must be 'demo' or 'production'.")
    expected_ack = (
        "I_UNDERSTAND_KALSHI_DEMO_ORDERS"
        if live.environment == "demo"
        else "I_UNDERSTAND_THIS_SUBMITS_REAL_KALSHI_PRODUCTION_ORDERS"
    )
    if live.acknowledgement != expected_ack:
        raise ValueError(f"Live trading requires acknowledgement = {expected_ack!r}.")
    _validate_live_base_url(live)
    if live.environment == "production" and not live.allow_production:
        raise ValueError("Production live trading requires live.allow_production=true.")
    if not live.auto_trade:
        raise ValueError("Live trading requires live.auto_trade=true; otherwise use paper mode or auth-check only.")
    if live.max_order_dollars <= 0:
        raise ValueError("live.max_order_dollars must be explicitly greater than 0.")
    if live.max_contracts < 1:
        raise ValueError("live.max_contracts must be explicitly at least 1.")
    if live.max_open_positions < 1:
        raise ValueError("live.max_open_positions must be explicitly at least 1.")
    if live.max_daily_orders < 1:
        raise ValueError("live.max_daily_orders must be explicitly at least 1.")
    if live.max_daily_loss_dollars <= 0:
        raise ValueError("live.max_daily_loss_dollars must be explicitly greater than 0.")
    if live.min_cash_reserve_dollars <= 0:
        raise ValueError("live.min_cash_reserve_dollars must be explicitly greater than 0.")
    if not (0.0 < live.max_spread < 1.0):
        raise ValueError("live.max_spread must be between 0 and 1.")
    if live.time_in_force != "immediate_or_cancel":
        raise ValueError("This bot only allows live.time_in_force='immediate_or_cancel'.")
    if live.min_seconds_between_orders < 1:
        raise ValueError("live.min_seconds_between_orders must be at least 1.")
    if not os.getenv("KALSHI_API_KEY_ID"):
        raise ValueError("Live mode requires KALSHI_API_KEY_ID in the environment or .env.")
    if not os.getenv("KALSHI_PRIVATE_KEY_FILE"):
        raise ValueError("Live mode requires KALSHI_PRIVATE_KEY_FILE in the environment or .env.")


def _validate_live_base_url(live: LiveConfig) -> None:
    parsed = urlparse(live.base_url)
    expected_hosts = {
        "demo": "external-api.demo.kalshi.co",
        "production": "external-api.kalshi.com",
    }
    expected_host = expected_hosts[live.environment]
    if (
        parsed.scheme != "https"
        or parsed.netloc != expected_host
        or parsed.path.rstrip("/") != "/trade-api/v2"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "live.base_url must be the official Kalshi "
            f"{live.environment} HTTPS trade API endpoint: https://{expected_host}/trade-api/v2"
        )
