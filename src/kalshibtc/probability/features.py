from __future__ import annotations

from dataclasses import dataclass

from ..market.state import MarketState


@dataclass(frozen=True)
class ProbabilityFeatures:
    distance_to_strike: float
    seconds_to_close: float
    atr: float
    atr_slope: float
    realized_volatility: float
    ema_slope: float
    vwap_slope: float
    recent_momentum: float
    distance_from_vwap: float
    wickiness: float
    range_expansion: float
    orderbook_imbalance: float
    z_score: float

    def vector(self) -> list[float]:
        return [
            self.z_score,
            self.atr_slope,
            self.realized_volatility,
            self.ema_slope,
            self.vwap_slope,
            self.recent_momentum,
            self.distance_from_vwap,
            self.wickiness,
            self.range_expansion,
            self.orderbook_imbalance,
        ]


@dataclass(frozen=True)
class ProbabilityExample:
    features: ProbabilityFeatures
    label_finish_above: bool
    realized_move_to_settlement: float


def extract_probability_features(state: MarketState) -> ProbabilityFeatures:
    raw = dict(state.tick.raw or {})
    raw.update(dict(state.orderbook.raw or {}))
    distance = float(state.price - state.strike)
    seconds = max(0.0, float(state.seconds_to_close))
    atr = _first_float(raw, "atr_60s", "atr", "atr_1m", default=max(abs(distance), 1.0))
    atr_slope = _first_float(raw, "atr_slope", "atr_expansion_rate", default=0.0)
    realized_vol = _first_float(raw, "realized_volatility", "realized_vol", default=0.0)
    ema_slope = _first_float(raw, "ema_slope", "ema_vwap_slope", default=state.slope_30s or 0.0)
    vwap_slope = _first_float(raw, "vwap_slope", default=0.0)
    recent_momentum = _first_float(raw, "recent_momentum", "momentum", "btc_velocity_30s", default=state.slope_30s or 0.0)
    vwap = _first_float(raw, "vwap", default=state.price)
    wickiness = _first_float(raw, "wickiness", "chop_score", default=0.0)
    range_expansion = _first_float(raw, "range_expansion", default=1.0)
    orderbook_imbalance = _orderbook_imbalance(raw)
    z_denom = max(atr, 1e-9) * max(seconds, 1.0) ** 0.5
    return ProbabilityFeatures(
        distance_to_strike=distance,
        seconds_to_close=seconds,
        atr=atr,
        atr_slope=atr_slope,
        realized_volatility=realized_vol,
        ema_slope=ema_slope,
        vwap_slope=vwap_slope,
        recent_momentum=recent_momentum,
        distance_from_vwap=float(state.price - vwap),
        wickiness=wickiness,
        range_expansion=range_expansion,
        orderbook_imbalance=orderbook_imbalance,
        z_score=distance / z_denom,
    )


def build_probability_example(state: MarketState, *, final_price: float) -> ProbabilityExample:
    return ProbabilityExample(
        features=extract_probability_features(state),
        label_finish_above=float(final_price) > state.strike,
        realized_move_to_settlement=float(final_price) - state.price,
    )


def _orderbook_imbalance(raw: dict[str, object]) -> float:
    yes_bid_size = _first_float(raw, "yes_bid_size", default=0.0)
    yes_ask_size = _first_float(raw, "yes_ask_size", default=0.0)
    no_bid_size = _first_float(raw, "no_bid_size", default=0.0)
    no_ask_size = _first_float(raw, "no_ask_size", default=0.0)
    total = yes_bid_size + yes_ask_size + no_bid_size + no_ask_size
    if total <= 0:
        return 0.0
    return (yes_bid_size - yes_ask_size) / total


def _first_float(raw: dict[str, object], *keys: str, default: float) -> float:
    for key in keys:
        value = raw.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return float(default)
