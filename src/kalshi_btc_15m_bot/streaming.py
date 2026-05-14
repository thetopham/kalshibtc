from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from .bot import KalshiBTC15MBot, dump_json
from .kalshi_client import KalshiPublicClient
from .ledger import evaluate_paper_exit, mark_open_trade_to_market, row_to_dict
from .live import KalshiAuthenticatedClient, KalshiCredentialError
from .market_data import candles_to_frame
from .models import KalshiMarket, Prediction, now_utc, parse_ts
from .supabase_features import feature_client_from_config

MIN_MONITOR_EDGE = 0.01
MIN_EV_PER_DOLLAR = 0.08
MIN_PROB_EDGE = 0.03
MAX_SPREAD = 0.04
EV_REFERENCE_RISK_DOLLARS = 25.0
EXECUTION_BASE_RISK_DOLLARS = 25.0
EXECUTION_MAX_SIZE_DOLLARS = 25.0
EXECUTION_MIN_SECONDS_TO_CLOSE = 45.0
EXECUTION_STOP_LOSS_CENTS = 0.10
EXECUTION_TAKE_PROFIT_CENTS = 0.12
EXECUTION_TREND_VELOCITY_DOLLARS_PER_SECOND = 0.25
EXECUTION_NEAR_STRIKE_DOLLARS = 25.0
BTC_HISTORY_RETENTION_SECONDS = 180.0
BTC_TICK_STALE_SECONDS = 5.0
ORDERBOOK_STALE_SECONDS = 60.0
ROLLOVER_REFRESH_LEAD_SECONDS = 30.0
ROLLOVER_RETRY_BEFORE_CLOSE_SECONDS = 2.0
ROLLOVER_RETRY_AFTER_CLOSE_SECONDS = 1.0
INITIAL_MARKET_REFRESH_BACKOFF_SECONDS = 2.0
MAX_MARKET_REFRESH_BACKOFF_SECONDS = 30.0
NO_TRADE_WARNING_DECISIONS = {
    "market_near_close_pending_rollover": "NO_TRADE_ROLLOVER_UNSAFE",
    "market_closed_pending_rollover": "NO_TRADE_ROLLOVER_UNSAFE",
    "market_rollover_refresh_failed": "NO_TRADE_ROLLOVER_UNSAFE",
    "invalid_orderbook_quotes": "WATCH_ONLY_INVALID_ORDERBOOK",
    "btc_tick_stale": "WATCH_ONLY_STALE_MARKET_DATA",
    "orderbook_quotes_stale": "WATCH_ONLY_STALE_MARKET_DATA",
    "btc_ws_reconnect_failed": "WATCH_ONLY_STREAM_RECONNECTING",
    "kalshi_ws_reconnect_failed": "WATCH_ONLY_STREAM_RECONNECTING",
    "kalshi_ws_auth_reconnect_failed": "WATCH_ONLY_STREAM_RECONNECTING",
    "model_state_probability_gap": "WATCH_ONLY_MODEL_DISAGREEMENT",
    "market_probability_gap": "WATCH_ONLY_MARKET_DISAGREEMENT",
    "supabase_features_stale": "WATCH_ONLY_STALE_SUPABASE_FEATURES",
    "supabase_features_error": "WATCH_ONLY_SUPABASE_FEATURE_ERROR",
}
EXECUTION_BLOCKING_WARNINGS = {
    "invalid_orderbook_quotes",
    "market_near_close_pending_rollover",
    "market_closed_pending_rollover",
    "market_rollover_refresh_failed",
    "btc_tick_stale",
    "orderbook_quotes_stale",
    "btc_ws_reconnect_failed",
    "kalshi_ws_reconnect_failed",
    "kalshi_ws_auth_reconnect_failed",
    "supabase_features_stale",
    "supabase_features_error",
}
STREAM_DATA_BLOCKING_WARNINGS = {
    "invalid_orderbook_quotes",
    "market_near_close_pending_rollover",
    "market_closed_pending_rollover",
    "market_rollover_refresh_failed",
    "btc_tick_stale",
    "orderbook_quotes_stale",
    "btc_ws_reconnect_failed",
    "kalshi_ws_reconnect_failed",
    "kalshi_ws_auth_reconnect_failed",
}


@dataclass(frozen=True)
class BtcTick:
    source: str
    product: str
    price: float
    bid: float | None
    ask: float | None
    ts: datetime


class KalshiOrderBook:
    """Mutable Kalshi binary order book view keyed by YES/NO bid levels.

    Kalshi publishes YES bids and NO bids. The cheapest YES ask is the complement
    of the best NO bid, and the cheapest NO ask is the complement of the best YES bid.
    """

    def __init__(self, market_ticker: str) -> None:
        self.market_ticker = market_ticker
        self.yes_levels: dict[float, float] = {}
        self.no_levels: dict[float, float] = {}
        self.last_seq: int | None = None
        self.updated_at: datetime | None = None

    @classmethod
    def from_snapshot(cls, market_ticker: str, msg: Mapping[str, Any]) -> KalshiOrderBook:
        book = cls(str(msg.get("market_ticker") or market_ticker))
        book.apply_snapshot(msg)
        return book

    def apply_snapshot(self, msg: Mapping[str, Any]) -> None:
        ticker = str(msg.get("market_ticker") or self.market_ticker)
        if ticker and ticker != self.market_ticker:
            self.market_ticker = ticker
        self.yes_levels = _levels_to_dict(_book_levels(msg, "yes"))
        self.no_levels = _levels_to_dict(_book_levels(msg, "no"))
        self.updated_at = _message_time(msg) or now_utc()

    def apply_delta(self, msg: Mapping[str, Any]) -> None:
        ticker = str(msg.get("market_ticker") or self.market_ticker)
        if ticker != self.market_ticker:
            return
        side = str(msg.get("side") or "").lower()
        levels = self.yes_levels if side == "yes" else self.no_levels if side == "no" else None
        if levels is None:
            return
        price = _as_float(msg.get("price_dollars") or msg.get("price"), default=0.0)
        delta = _as_float(msg.get("delta_fp") or msg.get("delta") or msg.get("count_delta"), default=0.0)
        if not (0.0 < price < 1.0) or abs(delta) <= 1e-9:
            return
        current = levels.get(price, 0.0) + delta
        if current <= 1e-9:
            levels.pop(price, None)
        else:
            levels[price] = current
        self.updated_at = _message_time(msg) or now_utc()

    @property
    def best_yes_bid(self) -> float | None:
        return max(self.yes_levels) if self.yes_levels else None

    @property
    def best_no_bid(self) -> float | None:
        return max(self.no_levels) if self.no_levels else None

    @property
    def yes_ask(self) -> float | None:
        return round(1.0 - self.best_no_bid, 4) if self.best_no_bid is not None else None

    @property
    def no_ask(self) -> float | None:
        return round(1.0 - self.best_yes_bid, 4) if self.best_yes_bid is not None else None

    @property
    def is_crossed(self) -> bool:
        """True when YES/NO bids imply impossible crossed complements.

        Kalshi asks are derived as the complement of the opposite side's bid. If
        best YES bid + best NO bid is greater than $1.00, the local view is
        stale/inconsistent and should not generate edge signals.
        """
        yes_bid = self.best_yes_bid
        no_bid = self.best_no_bid
        return yes_bid is not None and no_bid is not None and yes_bid + no_bid > 1.0 + 1e-9

    @property
    def visible_liquidity(self) -> float:
        return sum(price * count for price, count in self.yes_levels.items()) + sum(
            price * count for price, count in self.no_levels.items()
        )

    def to_market(self, market: KalshiMarket) -> KalshiMarket:
        yes_bid = self.best_yes_bid if self.best_yes_bid is not None else market.yes_bid
        no_bid = self.best_no_bid if self.best_no_bid is not None else market.no_bid
        yes_ask = self.yes_ask if self.yes_ask is not None else market.yes_ask
        no_ask = self.no_ask if self.no_ask is not None else market.no_ask
        return replace(
            market,
            yes_bid=round(yes_bid, 4),
            yes_ask=round(yes_ask, 4),
            no_bid=round(no_bid, 4),
            no_ask=round(no_ask, 4),
            liquidity=round(max(market.liquidity, self.visible_liquidity), 4),
        )


def build_realtime_state(
    prediction: Prediction,
    *,
    market: KalshiMarket,
    orderbook: KalshiOrderBook,
    btc: BtcTick,
    now: datetime | None = None,
    last_refresh_error: str | None = None,
    rollover_attempted: bool = False,
    rollover_status: str | None = None,
    rollover_old_ticker: str | None = None,
    rollover_retry_in_seconds: float | None = None,
    btc_history: Sequence[BtcTick] | None = None,
    stream_warnings: Sequence[str] | None = None,
) -> dict[str, Any]:
    now = now or now_utc()
    quoted_market = orderbook.to_market(market)
    yes_ask = quoted_market.yes_ask
    no_ask = quoted_market.no_ask
    expected_expiration = market.expected_expiration_time
    seconds_to_expected_expiration = (
        (expected_expiration - now).total_seconds() if expected_expiration else None
    )
    seconds_to_close = market.seconds_to_close(now)
    market_closed = seconds_to_close is not None and seconds_to_close <= 0
    stale_closed_seconds = abs(seconds_to_close) if market_closed and seconds_to_close is not None else None
    rollover_window = (
        seconds_to_close is not None and seconds_to_close <= ROLLOVER_REFRESH_LEAD_SECONDS
    )
    refresh_failed = bool(last_refresh_error)
    rollover_unsafe = rollover_window or refresh_failed
    orderbook_valid = not orderbook.is_crossed
    btc_tick_age_seconds = _datetime_age_seconds(now=now, ts=btc.ts)
    orderbook_age_seconds = _datetime_age_seconds(now=now, ts=orderbook.updated_at)
    btc_tick_stale = (
        btc_tick_age_seconds is not None and btc_tick_age_seconds > BTC_TICK_STALE_SECONDS
    )
    orderbook_stale = (
        orderbook_age_seconds is not None and orderbook_age_seconds > ORDERBOOK_STALE_SECONDS
    )
    stream_warning_list = list(stream_warnings or [])
    stream_reconnecting = any(warning in EXECUTION_BLOCKING_WARNINGS for warning in stream_warning_list)
    market_data_stale = btc_tick_stale or orderbook_stale or stream_reconnecting
    distance_to_target, distance_to_target_pct, direction_state = _target_distance(
        current_price=btc.price,
        target_price=market.target_price,
    )
    velocity_features = _btc_velocity_features(
        current=btc,
        target_price=market.target_price,
        history=btc_history or [],
        now=now,
    )
    probability_yes, probability_source, volatility_15m, distance_z = _close_state_probability_yes(
        current_price=btc.price,
        target_price=market.target_price,
        seconds_to_close=seconds_to_close,
        feature_snapshot=prediction.feature_snapshot,
        fallback_probability_yes=prediction.probability_yes,
    )
    probability_no = 1.0 - probability_yes
    regime = _classify_execution_regime(velocity_features=velocity_features)
    quotes_usable = orderbook_valid and not rollover_unsafe and not market_data_stale
    edge_yes = probability_yes - yes_ask if quotes_usable and 0.0 < yes_ask < 1.0 else None
    edge_no = probability_no - no_ask if quotes_usable and 0.0 < no_ask < 1.0 else None
    best_probability_edge_side, best_probability_edge = _best_side(edge_yes=edge_yes, edge_no=edge_no)
    ev_yes_per_contract = _binary_ev_per_contract(
        probability=probability_yes if quotes_usable else None,
        ask=yes_ask if quotes_usable else None,
    )
    ev_no_per_contract = _binary_ev_per_contract(
        probability=probability_no if quotes_usable else None,
        ask=no_ask if quotes_usable else None,
    )
    ev_yes_per_dollar = _binary_ev_per_dollar(
        probability=probability_yes if quotes_usable else None,
        ask=yes_ask if quotes_usable else None,
    )
    ev_no_per_dollar = _binary_ev_per_dollar(
        probability=probability_no if quotes_usable else None,
        ask=no_ask if quotes_usable else None,
    )
    best_ev_side, best_ev_per_dollar = _best_side(
        edge_yes=ev_yes_per_dollar,
        edge_no=ev_no_per_dollar,
    )
    best_ev_per_contract = _side_value(
        best_ev_side,
        yes=ev_yes_per_contract,
        no=ev_no_per_contract,
    )
    best_side = best_ev_side
    best_edge = _side_value(best_ev_side, yes=edge_yes, no=edge_no)
    best_spread = _side_spread(best_ev_side, market=quoted_market)
    best_ev_reference_profit_dollars = (
        best_ev_per_dollar * EV_REFERENCE_RISK_DOLLARS if best_ev_per_dollar is not None else None
    )
    market_implied_yes = _market_implied_yes(quoted_market) if quotes_usable else None
    warnings: list[str] = []
    if rollover_window:
        warnings.append(
            "market_closed_pending_rollover" if market_closed else "market_near_close_pending_rollover"
        )
    if refresh_failed:
        warnings.append("market_rollover_refresh_failed")
    if not orderbook_valid:
        warnings.append("invalid_orderbook_quotes")
    if btc_tick_stale:
        warnings.append("btc_tick_stale")
    if orderbook_stale:
        warnings.append("orderbook_quotes_stale")
    warnings.extend(stream_warning_list)
    warnings.extend(
        _state_warnings(
            direction_state=direction_state,
            best_side=best_side,
            best_edge=best_edge,
            probability_yes=probability_yes,
            model_probability_yes=prediction.probability_yes,
            market_implied_yes=market_implied_yes,
        )
    )
    if "edge_direction_disagreement" in warnings and "countertrend_ev_watch" not in warnings:
        warnings.append("countertrend_ev_watch")
    warnings = _dedupe_preserving_order(warnings)
    decision = _stream_decision(
        best_ev_side=best_ev_side,
        best_ev_per_dollar=best_ev_per_dollar,
        best_edge=best_edge,
        best_spread=best_spread,
        warnings=warnings,
    )
    monitor_action = (
        _monitor_action(
            best_side=best_ev_side,
            best_edge=best_edge,
            best_ev_per_dollar=best_ev_per_dollar,
        )
        if decision == "WATCH_ONLY_EV_SIGNAL"
        else "NO_EDGE"
    )
    monitor_side = best_ev_side if monitor_action != "NO_EDGE" else "NONE"
    execution_decision = _execution_decision(
        current_price=btc.price,
        target_price=market.target_price,
        seconds_to_close=seconds_to_close,
        yes_ask=yes_ask,
        no_ask=no_ask,
        yes_spread=_side_spread("YES", market=quoted_market),
        no_spread=_side_spread("NO", market=quoted_market),
        regime=regime,
        warnings=warnings,
        velocity_features=velocity_features,
    )
    return {
        "event": "market_state",
        "as_of": now.isoformat(),
        "btc_source": btc.source,
        "btc_product": btc.product,
        "btc_ts": btc.ts.isoformat(),
        "btc_tick_age_seconds": btc_tick_age_seconds,
        "btc_tick_stale": btc_tick_stale,
        "current_price": btc.price,
        "btc_bid": btc.bid,
        "btc_ask": btc.ask,
        "market_ticker": market.ticker,
        "event_ticker": market.event_ticker,
        "target_price": market.target_price,
        "market_close_time": market.close_time.isoformat() if market.close_time else None,
        "market_expiration_time": expected_expiration.isoformat() if expected_expiration else None,
        "market_expected_expiration_time": expected_expiration.isoformat() if expected_expiration else None,
        "seconds_to_close": seconds_to_close,
        "seconds_to_expiration": seconds_to_expected_expiration,
        "seconds_to_expected_expiration": seconds_to_expected_expiration,
        "seconds_to_probability_cutoff": seconds_to_close,
        "probability_cutoff": "contract_close",
        "market_closed": market_closed,
        "market_rollover_unsafe": rollover_unsafe,
        "rollover_refresh_lead_seconds": ROLLOVER_REFRESH_LEAD_SECONDS,
        "rollover_attempted": bool(rollover_attempted),
        "rollover_status": rollover_status,
        "rollover_old_ticker": rollover_old_ticker or market.ticker,
        "rollover_retry_in_seconds": rollover_retry_in_seconds,
        "stale_closed_seconds": stale_closed_seconds,
        "distance_to_target": distance_to_target,
        "distance_to_target_pct": distance_to_target_pct,
        "direction_state": direction_state,
        "btc_velocity_30s": velocity_features.get("btc_velocity_30s"),
        "regime": regime,
        "probability_yes": probability_yes,
        "probability_no": probability_no,
        "probability_source": probability_source,
        "state_probability_yes": probability_yes,
        "state_probability_no": probability_no,
        "state_probability_vol_15m": volatility_15m,
        "state_probability_distance_z": distance_z,
        "model_probability_yes": prediction.probability_yes,
        "model_probability_no": prediction.probability_no,
        "model_probability_gap": probability_yes - prediction.probability_yes,
        "market_implied_yes": market_implied_yes,
        "market_probability_gap": probability_yes - market_implied_yes if market_implied_yes is not None else None,
        "confidence": abs(probability_yes - 0.5) * 2.0,
        "model_confidence": prediction.confidence,
        "yes_bid": quoted_market.yes_bid,
        "yes_ask": quoted_market.yes_ask,
        "no_bid": quoted_market.no_bid,
        "no_ask": quoted_market.no_ask,
        "orderbook_liquidity": orderbook.visible_liquidity,
        "liquidity": quoted_market.liquidity,
        "orderbook_valid": orderbook_valid,
        "orderbook_updated_at": orderbook.updated_at.isoformat() if orderbook.updated_at else None,
        "orderbook_age_seconds": orderbook_age_seconds,
        "orderbook_stale": orderbook_stale,
        "edge_yes": edge_yes,
        "edge_no": edge_no,
        "probability_edge_yes": edge_yes,
        "probability_edge_no": edge_no,
        "best_probability_edge_side": best_probability_edge_side,
        "best_probability_edge": best_probability_edge,
        "ev_yes_per_contract": ev_yes_per_contract,
        "ev_no_per_contract": ev_no_per_contract,
        "ev_yes_per_dollar": ev_yes_per_dollar,
        "ev_no_per_dollar": ev_no_per_dollar,
        "best_ev_side": best_ev_side,
        "best_ev_per_contract": best_ev_per_contract,
        "best_ev_per_dollar": best_ev_per_dollar,
        "best_spread": best_spread,
        "ev_reference_risk_dollars": EV_REFERENCE_RISK_DOLLARS,
        "best_ev_reference_profit_dollars": best_ev_reference_profit_dollars,
        "best_side": best_side,
        "best_edge": best_edge,
        "monitor_action": monitor_action,
        "monitor_side": monitor_side,
        "decision": decision,
        "execution_decision": execution_decision,
        "min_ev_per_dollar": MIN_EV_PER_DOLLAR,
        "min_probability_edge": MIN_PROB_EDGE,
        "max_spread": MAX_SPREAD,
        "stream_warnings": stream_warning_list,
        "market_data_stale": market_data_stale,
        "warnings": warnings,
        "last_refresh_error": last_refresh_error,
        "prediction_action": prediction.action,
        "prediction_side": prediction.side,
        "stake_dollars": prediction.stake_dollars,
        "reasons": prediction.reasons,
        "boundary": "read-only websocket market-state stream; no orders submitted",
    }


def _target_distance(*, current_price: float, target_price: float | None) -> tuple[float | None, float | None, str]:
    if target_price is None or not math.isfinite(target_price) or target_price <= 0:
        return None, None, "UNKNOWN"
    distance = current_price - target_price
    distance_pct = distance / target_price
    if abs(distance_pct) < 0.00001:
        direction = "AT_TARGET"
    elif distance > 0:
        direction = "ABOVE_TARGET"
    else:
        direction = "BELOW_TARGET"
    return distance, distance_pct, direction


def _datetime_age_seconds(*, now: datetime, ts: datetime | None) -> float | None:
    if ts is None:
        return None
    return max(0.0, (now - ts).total_seconds())


def _btc_velocity_features(
    *,
    current: BtcTick,
    target_price: float | None,
    history: Sequence[BtcTick],
    now: datetime,
) -> dict[str, Any]:
    points = sorted(
        [tick for tick in history if tick.ts <= current.ts and tick.price > 0],
        key=lambda tick: tick.ts,
    )
    return {"btc_velocity_30s": _price_velocity(points, current=current, window_seconds=30.0)}


def _prior_tick_for_window(
    history: list[BtcTick],
    *,
    current: BtcTick,
    window_seconds: float,
) -> BtcTick | None:
    if not history:
        return None
    cutoff = current.ts.timestamp() - window_seconds
    older = [tick for tick in history if tick.ts.timestamp() <= cutoff]
    if not older:
        return None
    return max(older, key=lambda tick: tick.ts)


def _price_velocity(history: list[BtcTick], *, current: BtcTick, window_seconds: float) -> float | None:
    prior = _prior_tick_for_window(history, current=current, window_seconds=window_seconds)
    if prior is None:
        return None
    elapsed = (current.ts - prior.ts).total_seconds()
    if elapsed <= 0:
        return None
    return (current.price - prior.price) / elapsed


def _classify_execution_regime(*, velocity_features: Mapping[str, Any]) -> str:
    """Classify the BTC tape using only the 30s slope."""
    velocity = _primary_btc_velocity(velocity_features)
    if velocity is None:
        return "flat_chop"
    if velocity >= EXECUTION_TREND_VELOCITY_DOLLARS_PER_SECOND:
        return "uptrend"
    if velocity <= -EXECUTION_TREND_VELOCITY_DOLLARS_PER_SECOND:
        return "downtrend"
    return "flat_chop"


def _primary_btc_velocity(velocity_features: Mapping[str, Any]) -> float | None:
    return _as_optional_float(velocity_features.get("btc_velocity_30s"))


def _execution_decision(
    *,
    current_price: float,
    target_price: float | None,
    seconds_to_close: float | None,
    yes_ask: float | None,
    no_ask: float | None,
    yes_spread: float | None,
    no_spread: float | None,
    regime: str,
    warnings: list[str],
    velocity_features: Mapping[str, Any],
) -> dict[str, Any]:
    """One-slope v1 decision: price vs strike + 30s BTC slope, then hard blockers."""

    blocked_by: list[str] = []
    side = "NONE"
    entry_price: float | None = None
    side_spread: float | None = None
    velocity_30s = _as_optional_float(velocity_features.get("btc_velocity_30s"))

    if target_price is None or not math.isfinite(target_price) or target_price <= 0:
        blocked_by.append("missing_target_price")
        distance_to_strike = None
    else:
        distance_to_strike = current_price - target_price
        if abs(distance_to_strike) < EXECUTION_NEAR_STRIKE_DOLLARS:
            blocked_by.append("chop_zone")

    if velocity_30s is None:
        blocked_by.append("missing_btc_velocity_30s")

    if seconds_to_close is None:
        blocked_by.append("missing_close_clock")
    elif seconds_to_close < EXECUTION_MIN_SECONDS_TO_CLOSE:
        blocked_by.append("too_close_to_expiry")

    for warning in warnings:
        if warning in EXECUTION_BLOCKING_WARNINGS:
            blocked_by.append(warning)

    if distance_to_strike is not None and velocity_30s is not None and "chop_zone" not in blocked_by:
        if distance_to_strike > 0 and velocity_30s >= EXECUTION_TREND_VELOCITY_DOLLARS_PER_SECOND:
            side = "YES"
            entry_price = yes_ask
            side_spread = yes_spread
        elif distance_to_strike < 0 and velocity_30s <= -EXECUTION_TREND_VELOCITY_DOLLARS_PER_SECOND:
            side = "NO"
            entry_price = no_ask
            side_spread = no_spread
        else:
            blocked_by.append("slope_not_aligned_with_strike")

    if side in {"YES", "NO"}:
        if entry_price is None or not (0.0 < entry_price < 1.0):
            blocked_by.append("invalid_entry_price")
        if side_spread is None or side_spread > MAX_SPREAD + 1e-9:
            blocked_by.append("spread_too_wide")
    elif not blocked_by:
        blocked_by.append("slope_not_aligned_with_strike")

    confidence = _slope_confidence(velocity_30s)
    blocked_by = _dedupe_preserving_order(blocked_by)
    if blocked_by:
        return _no_trade_execution_decision(regime=regime, confidence=confidence, blocked_by=blocked_by)

    action = f"BUY_{side}"
    size_dollars = _execution_size()
    return {
        "action": action,
        "side": side,
        "size_dollars": size_dollars,
        "entry_price": entry_price,
        "stop_type": "probability",
        "stop_price": _contract_stop_price(entry_price),
        "take_profit_price": _contract_take_profit_price(entry_price),
        "confidence": confidence,
        "regime": regime,
        "reason": _execution_reason(
            side=side,
            current_price=current_price,
            target_price=target_price,
            velocity_30s=velocity_30s,
        ),
        "blocked_by": [],
    }


def _no_trade_execution_decision(*, regime: str, confidence: float, blocked_by: list[str]) -> dict[str, Any]:
    primary = blocked_by[0] if blocked_by else "no_trade"
    return {
        "action": "NO_TRADE",
        "side": "NONE",
        "size_dollars": 0.0,
        "entry_price": None,
        "stop_type": "none",
        "stop_price": None,
        "take_profit_price": None,
        "confidence": confidence,
        "regime": regime,
        "reason": f"blocked by {primary}",
        "blocked_by": blocked_by,
    }


def _slope_confidence(velocity_30s: float | None) -> float:
    if velocity_30s is None:
        return 0.0
    strength = abs(velocity_30s) / (EXECUTION_TREND_VELOCITY_DOLLARS_PER_SECOND * 4.0)
    return round(max(0.0, min(1.0, strength)), 4)


def _execution_size() -> float:
    return round(min(EXECUTION_BASE_RISK_DOLLARS, EXECUTION_MAX_SIZE_DOLLARS), 2)


def _contract_stop_price(entry_price: float | None) -> float | None:
    if entry_price is None or not (0.0 < entry_price < 1.0):
        return None
    return round(max(0.01, entry_price - EXECUTION_STOP_LOSS_CENTS), 4)


def _contract_take_profit_price(entry_price: float | None) -> float | None:
    if entry_price is None or not (0.0 < entry_price < 1.0):
        return None
    return round(min(0.99, entry_price + EXECUTION_TAKE_PROFIT_CENTS), 4)


def _execution_reason(
    *,
    side: str,
    current_price: float,
    target_price: float | None,
    velocity_30s: float | None,
) -> str:
    strike = "unknown strike" if target_price is None else f"strike={target_price:.2f}"
    slope = "slope unavailable" if velocity_30s is None else f"slope_30s={velocity_30s:.2f}/s"
    relation = "above" if target_price is not None and current_price > target_price else "below"
    return f"{side}: price {relation} {strike}; {slope}"


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _as_optional_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _close_state_probability_yes(
    *,
    current_price: float,
    target_price: float | None,
    seconds_to_close: float | None,
    feature_snapshot: Mapping[str, Any],
    fallback_probability_yes: float,
) -> tuple[float, str, float | None, float | None]:
    """Estimate close-time YES probability from current distance, time left, and BTC volatility.

    The streaming view needs a *current-state* probability, not just the slower
    technical model's directional prior. Kalshi BTC 15m contracts stop trading at
    `close_time`, so the monitor uses contract close as the cutoff clock rather
    than the later expected-expiration metadata.
    """
    fallback = _clamp_probability(fallback_probability_yes)
    if (
        target_price is None
        or target_price <= 0
        or current_price <= 0
        or seconds_to_close is None
        or seconds_to_close <= 0
    ):
        return fallback, "model_fallback", None, None
    volatility_15m = _as_float(feature_snapshot.get("vol_16"), default=0.0)
    if not math.isfinite(volatility_15m) or volatility_15m <= 0:
        return fallback, "model_fallback_no_volatility", None, None
    volatility_15m = max(0.0005, min(0.02, volatility_15m))
    remaining_sigma = volatility_15m * math.sqrt(max(seconds_to_close, 1.0) / 900.0)
    if remaining_sigma <= 0:
        return fallback, "model_fallback_no_sigma", volatility_15m, None
    distance_z = math.log(current_price / target_price) / remaining_sigma
    probability_yes = 0.5 * (1.0 + math.erf(distance_z / math.sqrt(2.0)))
    return _clamp_probability(probability_yes), "close_distance_volatility", volatility_15m, distance_z


def _market_implied_yes(market: KalshiMarket) -> float | None:
    if 0.0 < market.yes_bid < market.yes_ask < 1.0:
        return (market.yes_bid + market.yes_ask) / 2.0
    if 0.0 < market.no_bid < market.no_ask < 1.0:
        return 1.0 - ((market.no_bid + market.no_ask) / 2.0)
    return None


def _binary_ev_per_dollar(*, probability: float | None, ask: float | None) -> float | None:
    if probability is None or ask is None:
        return None
    if not (0.0 < ask < 1.0):
        return None
    return probability / ask - 1.0


def _binary_ev_per_contract(*, probability: float | None, ask: float | None) -> float | None:
    if probability is None or ask is None:
        return None
    if not (0.0 < ask < 1.0):
        return None
    return probability - ask


def _side_value(side: str | None, *, yes: float | None, no: float | None) -> float | None:
    if side == "YES":
        return yes
    if side == "NO":
        return no
    return None


def _stream_execution_stake(paper_config: Any, *, execution_size: float) -> float:
    max_position = _as_optional_float(getattr(paper_config, "max_position_dollars", 25.0))
    if max_position is None or max_position <= 0:
        return 0.0
    if execution_size <= 0:
        return 0.0
    return float(round(min(execution_size, max_position), 2))


def _side_spread(side: str | None, *, market: KalshiMarket) -> float | None:
    if side == "YES" and 0.0 < market.yes_bid < market.yes_ask < 1.0:
        return market.yes_ask - market.yes_bid
    if side == "NO" and 0.0 < market.no_bid < market.no_ask < 1.0:
        return market.no_ask - market.no_bid
    return None


def _monitor_action(
    *,
    best_side: str | None,
    best_edge: float | None,
    best_ev_per_dollar: float | None,
) -> str:
    if (
        best_side in {"YES", "NO"}
        and best_edge is not None
        and best_edge >= MIN_MONITOR_EDGE
        and best_ev_per_dollar is not None
        and best_ev_per_dollar > 0.0
    ):
        return f"EV_{best_side}"
    return "NO_EDGE"


def _stream_decision(
    *,
    best_ev_side: str | None,
    best_ev_per_dollar: float | None,
    best_edge: float | None,
    best_spread: float | None,
    warnings: list[str],
) -> str:
    for warning, decision in NO_TRADE_WARNING_DECISIONS.items():
        if warning in warnings:
            return decision
    if best_ev_side not in {"YES", "NO"} or best_ev_per_dollar is None or best_edge is None:
        return "WATCH_ONLY_NO_EV"
    if best_ev_per_dollar <= 0.0 or best_edge <= 0.0:
        return "WATCH_ONLY_NO_POSITIVE_EV"
    if best_ev_per_dollar < MIN_EV_PER_DOLLAR:
        return "WATCH_ONLY_LOW_EV"
    if best_edge < MIN_PROB_EDGE:
        return "WATCH_ONLY_LOW_PROB_EDGE"
    if best_spread is None or best_spread > MAX_SPREAD:
        return "WATCH_ONLY_WIDE_SPREAD"
    return "WATCH_ONLY_EV_SIGNAL"


def _mark_research_warning(payload: dict[str, Any], warning: str, decision: str) -> None:
    warnings = payload.get("warnings")
    if isinstance(warnings, list):
        if warning not in warnings:
            warnings.append(warning)
    else:
        payload["warnings"] = [warning]
    payload["decision"] = decision
    payload["monitor_action"] = "NO_EDGE"
    payload["monitor_side"] = "NONE"


def _force_no_trade_warning(payload: dict[str, Any], warning: str, decision: str) -> None:
    _mark_research_warning(payload, warning, decision)
    raw_execution = payload.get("execution_decision")
    execution = raw_execution if isinstance(raw_execution, Mapping) else {}
    raw_blockers = execution.get("blocked_by") if isinstance(execution, Mapping) else None
    blockers = list(raw_blockers) if isinstance(raw_blockers, list) else []
    blockers.append(warning)
    payload["execution_decision"] = _no_trade_execution_decision(
        regime=str(execution.get("regime") or payload.get("regime") or "flat_chop"),
        confidence=_as_float(execution.get("confidence"), default=0.0),
        blocked_by=_dedupe_preserving_order(blockers),
    )


def _feature_client_from_bot(bot: KalshiBTC15MBot) -> Any | None:
    try:
        return feature_client_from_config(bot.config)
    except AttributeError:
        return None


def _state_warnings(
    *,
    direction_state: str,
    best_side: str | None,
    best_edge: float | None,
    probability_yes: float,
    model_probability_yes: float,
    market_implied_yes: float | None,
) -> list[str]:
    warnings: list[str] = []
    if best_edge is not None and best_edge > 0.03:
        if direction_state == "ABOVE_TARGET" and best_side == "NO":
            warnings.append("edge_direction_disagreement")
        elif direction_state == "BELOW_TARGET" and best_side == "YES":
            warnings.append("edge_direction_disagreement")
    if abs(probability_yes - model_probability_yes) >= 0.25:
        warnings.append("model_state_probability_gap")
    if market_implied_yes is not None and abs(probability_yes - market_implied_yes) >= 0.25:
        warnings.append("market_probability_gap")
    return warnings


def _clamp_probability(value: float) -> float:
    if not math.isfinite(value):
        return 0.5
    return max(0.01, min(0.99, value))


def format_realtime_state(payload: Mapping[str, Any]) -> str:
    raw_execution = payload.get("execution_decision")
    execution: Mapping[str, Any] = raw_execution if isinstance(raw_execution, Mapping) else {}
    action = str(execution.get("action") or "NO_TRADE")
    raw_blocked_by = execution.get("blocked_by")
    blocked_by = raw_blocked_by if isinstance(raw_blocked_by, list) else []
    lines = [
        "BTC 15m Kalshi execution_decision",
        (
            f"ACTION={action} side={execution.get('side', 'NONE')} "
            f"size={_fmt_signed_money(execution.get('size_dollars'))} "
            f"entry={_fmt_probability(execution.get('entry_price'))} "
            f"stop={_fmt_probability(execution.get('stop_price'))} "
            f"take_profit={_fmt_probability(execution.get('take_profit_price'))} "
            f"confidence={_fmt_probability(execution.get('confidence'))} "
            f"regime={execution.get('regime', payload.get('regime', 'flat_chop'))}"
        ),
        (
            f"market={payload.get('market_ticker')} target={payload.get('target_price')} "
            f"btc={float(payload.get('current_price') or 0.0):.2f} "
            f"slope_30s={_fmt_velocity(payload.get('btc_velocity_30s'))} "
            f"closes_in={_seconds_label(payload.get('seconds_to_close'))}"
        ),
        f"reason={execution.get('reason', 'no execution decision')}",
        "blocked_by=" + (",".join(str(item) for item in blocked_by) if blocked_by else "none"),
    ]
    warnings = payload.get("warnings")
    if isinstance(warnings, list) and warnings:
        lines.append(f"warnings={','.join(str(warning) for warning in warnings)}")
    if payload.get("rollover_attempted") and payload.get("rollover_status") == "same_market_returned":
        stale_seconds = payload.get("stale_closed_seconds")
        stale_part = f" stale_for={_seconds_label(stale_seconds)}" if stale_seconds is not None else ""
        lines.append(
            f"rollover=waiting old={payload.get('rollover_old_ticker') or payload.get('market_ticker')}"
            f"{stale_part} retry_in={_seconds_label(payload.get('rollover_retry_in_seconds'))}"
        )
    if payload.get("last_refresh_error"):
        lines.append(f"last_refresh_error={_fmt_error(payload.get('last_refresh_error'))}")
    if payload.get("stream_paper", {}).get("enabled"):
        trade_id = payload.get("paper_trade_id") or "none"
        skip = payload.get("paper_trade_skip_reason")
        lines.append(f"paper_trade_id={trade_id}" + (f" skip={skip}" if skip else ""))
    lines.append(f"boundary: {payload.get('boundary', 'read-only')}")
    return "\n".join(lines)


def _fmt_velocity(value: Any) -> str:
    numeric = _as_optional_float(value)
    return "n/a" if numeric is None else f"{numeric:+.2f}/s"


def kalshi_ws_url_from_rest_url(rest_url: str) -> str:
    parsed = urlparse(rest_url)
    host_map = {
        "external-api.kalshi.com": "external-api-ws.kalshi.com",
        "external-api.demo.kalshi.co": "external-api-ws.demo.kalshi.co",
        "api.elections.kalshi.com": "api.elections.kalshi.com",
        "demo-api.kalshi.co": "demo-api.kalshi.co",
    }
    host = host_map.get(parsed.netloc)
    if not host:
        raise ValueError(f"Unsupported Kalshi REST host for websocket mapping: {parsed.netloc}")
    return f"wss://{host}/trade-api/ws/v2"


def btc_ws_url_from_bot(bot: KalshiBTC15MBot) -> str:
    cfg = bot.config.market_data
    if cfg.provider.lower() == "binance":
        return f"wss://stream.binance.com:9443/ws/{cfg.symbol.lower()}@bookTicker"
    if cfg.provider.lower() == "coinbase":
        return "wss://ws-feed.exchange.coinbase.com"
    raise ValueError(f"Unsupported websocket market_data.provider: {cfg.provider}")


def parse_binance_book_ticker(payload: Mapping[str, Any]) -> BtcTick:
    data = _combined_payload(payload)
    bid = _as_float(data.get("b") or data.get("bidPrice"), default=0.0)
    ask = _as_float(data.get("a") or data.get("askPrice"), default=0.0)
    price = (bid + ask) / 2 if bid > 0 and ask > 0 else _as_float(data.get("c"), default=0.0)
    if price <= 0:
        raise ValueError("Binance bookTicker payload did not contain a usable BTC price")
    ts_ms = _as_float(data.get("E") or data.get("u"), default=0.0)
    ts = datetime.fromtimestamp(ts_ms / 1000, tz=UTC) if ts_ms > 0 else now_utc()
    return BtcTick(
        source="binance_ws",
        product=str(data.get("s") or "BTCUSDT"),
        price=price,
        bid=bid or None,
        ask=ask or None,
        ts=ts,
    )


def parse_coinbase_ticker(payload: Mapping[str, Any]) -> BtcTick:
    if str(payload.get("type") or "") not in {"ticker", "last_match", "match"}:
        raise ValueError("Coinbase payload is not a ticker/trade message")
    price = _as_float(payload.get("price"), default=0.0)
    bid = _as_float(payload.get("best_bid"), default=0.0)
    ask = _as_float(payload.get("best_ask"), default=0.0)
    if price <= 0 and bid > 0 and ask > 0:
        price = (bid + ask) / 2
    if price <= 0:
        raise ValueError("Coinbase ticker payload did not contain a usable BTC price")
    return BtcTick(
        source="coinbase_ws",
        product=str(payload.get("product_id") or "BTC-USD"),
        price=price,
        bid=bid or None,
        ask=ask or None,
        ts=parse_ts(str(payload.get("time") or "")) or now_utc(),
    )


class RealtimeStateStreamer:
    """Read-only websocket market-state loop.

    The loop updates BTC price and Kalshi top-of-book state from websocket events,
    recomputes the current YES/NO probabilities against the fresh order book, and
    emits operator-readable state. It intentionally does not call order submission.
    """

    def __init__(
        self,
        bot: KalshiBTC15MBot,
        *,
        json_output: bool = False,
        max_events: int | None = None,
        emit_min_interval_seconds: float = 1.0,
        emit: Callable[[str], None] | None = None,
        clock: Callable[[], datetime] = now_utc,
        paper_trading: bool = False,
        feature_client: Any | None = None,
    ) -> None:
        if emit_min_interval_seconds < 0:
            raise ValueError("emit_min_interval_seconds must be >= 0")
        if max_events is not None and max_events < 1:
            raise ValueError("max_events must be at least 1 when provided")
        self.bot = bot
        self.json_output = json_output
        self.max_events = max_events
        self.emit_min_interval_seconds = emit_min_interval_seconds
        self.emit = emit or (lambda text: print(text, flush=True))
        self.clock = clock
        self.paper_trading = paper_trading
        self.feature_client = feature_client if feature_client is not None else _feature_client_from_bot(bot)
        self.market: KalshiMarket | None = None
        self.orderbook: KalshiOrderBook | None = None
        self.btc: BtcTick | None = None
        self.frame = None
        self._last_emit_monotonic = 0.0
        self._emitted = 0
        self._next_market_refresh_monotonic = 0.0
        self._market_refresh_backoff_seconds = INITIAL_MARKET_REFRESH_BACKOFF_SECONDS
        self._next_rollover_refresh_monotonic = 0.0
        self._rollover_refresh_backoff_seconds = ROLLOVER_RETRY_BEFORE_CLOSE_SECONDS
        self._rollover_attempted = False
        self._rollover_status: str | None = None
        self._rollover_old_ticker: str | None = None
        self._last_refresh_error: str | None = None
        self._btc_history: list[BtcTick] = []
        self._btc_stream_warning: str | None = None
        self._kalshi_stream_warning: str | None = None

    def bootstrap(self) -> None:
        self.refresh_market(force=True)
        candles = self.bot.market_data.fetch_candles(self.bot.config.market_data.lookback_days)
        self.frame = candles_to_frame(
            candles,
            granularity_seconds=self.bot.config.market_data.granularity_seconds,
            drop_incomplete=True,
        )
        price = self.bot.market_data.current_price()
        self.btc = BtcTick(
            source="rest_bootstrap",
            product=self._btc_product_label(),
            price=price,
            bid=None,
            ask=None,
            ts=self.clock(),
        )
        self._record_btc_tick(self.btc)
        self.emit_state(force=True)

    def refresh_market(
        self,
        *,
        force: bool = False,
        rollover: bool = False,
        seconds_to_close: float | None = None,
    ) -> bool:
        """Refresh the active Kalshi contract and REST order book.

        Returns True when the streamer moved to a different market ticker, or
        when a forced initial refresh populated the market for the first time.
        Refresh failures are process-fail-open but signal-fail-closed: keep the
        previous market/book so the websocket loop stays alive, record the
        error for operator output, suppress edge output, and schedule bounded
        backoff. Rollover refreshes use their own near-close cadence so stale
        market discovery is not delayed by the normal REST refresh backoff.
        """
        now_monotonic = time.monotonic()
        if not force:
            next_refresh = (
                self._next_rollover_refresh_monotonic
                if rollover
                else self._next_market_refresh_monotonic
            )
            if now_monotonic < next_refresh:
                return False

        previous_ticker = self.market.ticker if self.market is not None else None
        try:
            market = self.bot.kalshi.current_btc15m_market(
                self.bot.config.kalshi.series_ticker,
                status=self.bot.config.kalshi.market_status,
            )
        except Exception as exc:
            if rollover:
                self._schedule_rollover_refresh_backoff(now_monotonic, exc, seconds_to_close)
            else:
                self._schedule_market_refresh_backoff(now_monotonic, exc)
            return False

        if not force and previous_ticker == market.ticker:
            if rollover:
                self._mark_rollover_same_market(
                    now_monotonic=now_monotonic,
                    previous_ticker=previous_ticker,
                    seconds_to_close=seconds_to_close,
                )
            else:
                self._reset_rollover_state()
                self._last_refresh_error = None
                self._market_refresh_backoff_seconds = INITIAL_MARKET_REFRESH_BACKOFF_SECONDS
                self._next_market_refresh_monotonic = (
                    now_monotonic + INITIAL_MARKET_REFRESH_BACKOFF_SECONDS
                )
            return False

        try:
            orderbook = _rest_orderbook(self.bot.kalshi, market.ticker)
        except Exception as exc:
            if rollover:
                self._schedule_rollover_refresh_backoff(now_monotonic, exc, seconds_to_close)
            else:
                self._schedule_market_refresh_backoff(now_monotonic, exc)
            return False

        self.market = market
        self.orderbook = orderbook
        self._last_emit_monotonic = 0.0
        self._next_market_refresh_monotonic = 0.0
        self._market_refresh_backoff_seconds = INITIAL_MARKET_REFRESH_BACKOFF_SECONDS
        self._last_refresh_error = None
        self._reset_rollover_state()
        return force or previous_ticker != market.ticker

    def _schedule_market_refresh_backoff(self, now_monotonic: float, exc: Exception) -> None:
        self._last_refresh_error = _fmt_error(exc)
        self._next_market_refresh_monotonic = now_monotonic + self._market_refresh_backoff_seconds
        self._market_refresh_backoff_seconds = min(
            self._market_refresh_backoff_seconds * 2.0,
            MAX_MARKET_REFRESH_BACKOFF_SECONDS,
        )

    def _schedule_rollover_refresh_backoff(
        self,
        now_monotonic: float,
        exc: Exception,
        seconds_to_close: float | None,
    ) -> None:
        was_refresh_failed = self._rollover_status == "refresh_failed"
        self._rollover_attempted = True
        self._rollover_status = "refresh_failed"
        self._rollover_old_ticker = self.market.ticker if self.market is not None else None
        self._last_refresh_error = _fmt_error(exc)
        cadence = _rollover_refresh_cadence(seconds_to_close)
        if _is_rate_limit_error(exc):
            delay = self._rollover_refresh_backoff_seconds if was_refresh_failed else cadence
            if delay < cadence:
                delay = cadence
            self._rollover_refresh_backoff_seconds = min(
                max(delay * 2.0, cadence),
                MAX_MARKET_REFRESH_BACKOFF_SECONDS,
            )
        else:
            delay = cadence
            self._rollover_refresh_backoff_seconds = cadence
        self._next_rollover_refresh_monotonic = now_monotonic + delay

    def _mark_rollover_same_market(
        self,
        *,
        now_monotonic: float,
        previous_ticker: str | None,
        seconds_to_close: float | None,
    ) -> None:
        self._rollover_attempted = True
        self._rollover_status = "same_market_returned"
        self._rollover_old_ticker = previous_ticker
        self._last_refresh_error = None
        cadence = _rollover_refresh_cadence(seconds_to_close)
        self._rollover_refresh_backoff_seconds = cadence
        self._next_rollover_refresh_monotonic = now_monotonic + cadence

    def _reset_rollover_state(self) -> None:
        self._next_rollover_refresh_monotonic = 0.0
        self._rollover_refresh_backoff_seconds = ROLLOVER_RETRY_BEFORE_CLOSE_SECONDS
        self._rollover_attempted = False
        self._rollover_status = None
        self._rollover_old_ticker = None

    def _rollover_retry_in_seconds(self) -> float | None:
        if not self._rollover_attempted:
            return None
        return max(0.0, self._next_rollover_refresh_monotonic - time.monotonic())

    def refresh_market_if_closed(self, *, now: datetime | None = None) -> bool:
        """Roll to the next Kalshi contract shortly before the current contract closes."""
        if self.market is None:
            return self.refresh_market()
        seconds_to_close = self.market.seconds_to_close(now or self.clock())
        if seconds_to_close is None or seconds_to_close > ROLLOVER_REFRESH_LEAD_SECONDS:
            self._reset_rollover_state()
            return False
        return self.refresh_market(rollover=True, seconds_to_close=seconds_to_close)

    def apply_btc_tick(self, tick: BtcTick) -> None:
        self._btc_stream_warning = None
        self.btc = tick
        self._record_btc_tick(tick)
        self.emit_state()

    def _record_btc_tick(self, tick: BtcTick) -> None:
        self._btc_history.append(tick)
        cutoff = tick.ts.timestamp() - BTC_HISTORY_RETENTION_SECONDS
        self._btc_history = [item for item in self._btc_history if item.ts.timestamp() >= cutoff]

    def apply_kalshi_message(self, message: Mapping[str, Any]) -> None:
        if self.orderbook is None:
            return
        msg_type = str(message.get("type") or "")
        msg = message.get("msg") if isinstance(message.get("msg"), Mapping) else message
        if not isinstance(msg, Mapping):
            return
        if msg_type == "orderbook_snapshot":
            self.orderbook.apply_snapshot(msg)
        elif msg_type == "orderbook_delta":
            self.orderbook.apply_delta(msg)
        else:
            return
        self._kalshi_stream_warning = None
        seq = message.get("seq")
        if isinstance(seq, int):
            self.orderbook.last_seq = seq
        self.emit_state()

    def emit_state(self, *, force: bool = False) -> None:
        if self.market is None or self.orderbook is None or self.btc is None or self.frame is None:
            return
        now = self.clock()
        self.refresh_market_if_closed(now=now)
        if self.market is None or self.orderbook is None:
            return
        now_monotonic = time.monotonic()
        if not force and now_monotonic - self._last_emit_monotonic < self.emit_min_interval_seconds:
            return
        quoted_market = self.orderbook.to_market(self.market)
        prediction = self.bot.predictor.predict(
            self.frame,
            market=quoted_market,
            current_price=self.btc.price,
            now=now,
        )
        supabase_features, supabase_feature_error = self._fetch_supabase_features(
            now=now,
            quoted_market=quoted_market,
        )
        if supabase_features is not None:
            prediction = replace(
                prediction,
                feature_snapshot={
                    **prediction.feature_snapshot,
                    **supabase_features.to_feature_snapshot(),
                },
                reasons=[
                    *prediction.reasons,
                    (
                        "supabase_features: "
                        f"age={_seconds_label(supabase_features.feature_age_seconds)}; "
                        f"stale={supabase_features.stale}"
                    ),
                ],
            )
        payload = build_realtime_state(
            prediction,
            market=quoted_market,
            orderbook=self.orderbook,
            btc=self.btc,
            now=now,
            last_refresh_error=self._last_refresh_error,
            rollover_attempted=self._rollover_attempted,
            rollover_status=self._rollover_status,
            rollover_old_ticker=self._rollover_old_ticker,
            rollover_retry_in_seconds=self._rollover_retry_in_seconds(),
            btc_history=self._btc_history,
            stream_warnings=self._active_stream_warnings(),
        )
        self._attach_supabase_features(payload, supabase_features, supabase_feature_error)
        if self.paper_trading:
            self._apply_stream_paper(prediction, quoted_market=quoted_market, payload=payload, now=now)
        self.emit(dump_json(payload) if self.json_output else format_realtime_state(payload))
        self._last_emit_monotonic = now_monotonic
        self._emitted += 1

    def _active_stream_warnings(self) -> list[str]:
        warnings = [self._btc_stream_warning, self._kalshi_stream_warning]
        return [warning for warning in warnings if warning]

    def _fetch_supabase_features(
        self,
        *,
        now: datetime,
        quoted_market: KalshiMarket,
    ) -> tuple[Any | None, str | None]:
        if self.feature_client is None:
            return None, None
        try:
            return (
                self.feature_client.fetch_latest_btc_1m(
                    now=now,
                    current_price=self.btc.price if self.btc is not None else 0.0,
                    target_price=quoted_market.target_price,
                    seconds_to_close=quoted_market.seconds_to_close(now),
                ),
                None,
            )
        except Exception as exc:  # noqa: BLE001 - stream should stay alive and fail closed.
            return None, _fmt_error(exc)

    def _attach_supabase_features(
        self,
        payload: dict[str, Any],
        supabase_features: Any | None,
        supabase_feature_error: str | None,
    ) -> None:
        payload.setdefault("feature_source", "model_features")
        payload.setdefault("feature_stale", False)
        payload.setdefault("supabase_features", None)
        if supabase_features is not None:
            payload["feature_source"] = "supabase_tv_datafeed"
            payload["feature_stale"] = bool(supabase_features.stale)
            payload["supabase_features"] = supabase_features.to_jsonable()
            if supabase_features.stale:
                _force_no_trade_warning(
                    payload,
                    "supabase_features_stale",
                    "WATCH_ONLY_STALE_SUPABASE_FEATURES",
                )
            return
        if supabase_feature_error:
            payload["feature_source"] = "supabase_tv_datafeed"
            payload["feature_stale"] = True
            payload["supabase_feature_error"] = supabase_feature_error
            _force_no_trade_warning(
                payload,
                "supabase_features_error",
                "WATCH_ONLY_SUPABASE_FEATURE_ERROR",
            )

    def _apply_stream_paper(
        self,
        prediction: Prediction,
        *,
        quoted_market: KalshiMarket,
        payload: dict[str, Any],
        now: datetime,
    ) -> None:
        """Apply local paper-ledger actions to a freshly computed realtime state.

        This is deliberately paper-only. It records a synthetic prediction and
        opens/closes local ledger rows; it never touches the live-order adapter.
        """
        payload["boundary"] = "websocket paper trader; local paper ledger only; no live orders submitted"
        payload["paper_trade_id"] = None
        payload["paper_trade_skip_reason"] = None
        payload["stream_paper"] = {
            "enabled": True,
            "opened_side": None,
            "managed_positions": [],
        }

        execution = payload.get("execution_decision") if isinstance(payload.get("execution_decision"), Mapping) else {}
        execution_action = str(execution.get("action") or "NO_TRADE") if isinstance(execution, Mapping) else "NO_TRADE"
        raw_blocked_by = execution.get("blocked_by") if isinstance(execution, Mapping) else None
        execution_blockers = list(raw_blocked_by) if isinstance(raw_blocked_by, list) else []
        if payload.get("orderbook_valid") is not True or payload.get("market_rollover_unsafe") is True:
            blockers = ",".join(str(item) for item in execution_blockers) if execution_blockers else "none"
            payload["paper_trade_skip_reason"] = f"execution_decision: {execution_action} blocked_by={blockers}"
            return
        if any(blocker in STREAM_DATA_BLOCKING_WARNINGS for blocker in execution_blockers):
            blockers = ",".join(str(item) for item in execution_blockers) if execution_blockers else "none"
            payload["paper_trade_skip_reason"] = f"execution_decision: {execution_action} blocked_by={blockers}"
            return

        payload["stream_paper"]["managed_positions"] = self._manage_stream_paper_positions(
            quoted_market,
            now=now,
        )
        if execution_action not in {"BUY_YES", "BUY_NO"}:
            blocked_by = execution.get("blocked_by") if isinstance(execution, Mapping) else None
            blockers = ",".join(str(item) for item in blocked_by) if isinstance(blocked_by, list) else "none"
            payload["paper_trade_skip_reason"] = f"execution_decision: {execution_action} blocked_by={blockers}"
            return

        side = str(execution.get("side") or execution_action.removeprefix("BUY_")) if isinstance(execution, Mapping) else ""
        if side not in {"YES", "NO"}:
            payload["paper_trade_skip_reason"] = "execution_signal_missing_side"
            return
        ask = quoted_market.yes_ask if side == "YES" else quoted_market.no_ask
        if not (0.0 < ask < 1.0):
            payload["paper_trade_skip_reason"] = "execution_signal_invalid_ask"
            return

        execution_size = _as_float(execution.get("size_dollars"), default=0.0) if isinstance(execution, Mapping) else 0.0
        stake = _stream_execution_stake(self.bot.config.paper, execution_size=execution_size)
        if stake <= 0:
            payload["paper_trade_skip_reason"] = "execution_signal_zero_size"
            return
        paper_prediction = replace(
            prediction,
            prediction_id=f"stream-{uuid.uuid4()}",
            created_at=now,
            market=quoted_market,
            current_price=float(payload.get("current_price") or prediction.current_price),
            probability_yes=_as_float(payload.get("probability_yes"), default=prediction.probability_yes),
            probability_no=_as_float(payload.get("probability_no"), default=prediction.probability_no),
            action=f"BUY_{side}",
            side=side,
            edge=_as_float(payload.get("best_edge"), default=0.0),
            confidence=_as_float(execution.get("confidence"), default=prediction.confidence) if isinstance(execution, Mapping) else prediction.confidence,
            stake_dollars=stake,
            reasons=[
                *prediction.reasons,
                (
                    "stream_paper_execution: "
                    f"action={execution_action}; side={side}; "
                    f"size={stake:.2f}; regime={execution.get('regime') if isinstance(execution, Mapping) else 'unknown'}; "
                    f"edge={_fmt_edge(payload.get('best_edge'))}; "
                    f"ev_per_dollar={_fmt_ev_pct(payload.get('best_ev_per_dollar'))}"
                ),
            ],
        )
        self.bot.ledger.record_prediction(paper_prediction)
        trade_id = self.bot.ledger.maybe_open_paper_trade(paper_prediction)
        if trade_id is None:
            payload["paper_trade_skip_reason"] = (
                self.bot.ledger.paper_entry_skip_reason(paper_prediction) or "paper_entry_rejected"
            )
            return
        payload["paper_trade_id"] = trade_id
        payload["stream_paper"]["opened_side"] = side

    def _manage_stream_paper_positions(self, quoted_market: KalshiMarket, *, now: datetime) -> list[dict[str, Any]]:
        managed: list[dict[str, Any]] = []
        for row in self.bot.ledger.open_trades():
            data = row_to_dict(row)
            if data["market_ticker"] != quoted_market.ticker:
                continue
            try:
                mark = mark_open_trade_to_market(data, quoted_market)
                exit_signal = evaluate_paper_exit(data, mark, self.bot.config.paper, now=now)
                mark["exit_signal"] = exit_signal or "hold"
                if exit_signal:
                    mark["paper_closed"] = bool(
                        self.bot.ledger.close_paper_trade(
                            data["id"],
                            exit_price=float(mark["mark_price"]),
                            exit_reason=f"stream_{exit_signal}",
                            closed_at=now,
                        )
                    )
                else:
                    mark["paper_closed"] = False
                managed.append(mark)
            except Exception as exc:  # noqa: BLE001 - keep the stream alive and auditable.
                data["quote_error"] = _fmt_error(exc)
                data["exit_signal"] = "unknown"
                data["paper_closed"] = False
                managed.append(data)
        return managed

    async def run(self) -> int:
        self.bootstrap()
        if self._max_events_reached():
            return 0
        stop = asyncio.Event()
        try:
            await asyncio.gather(self._btc_ws_loop(stop), self._kalshi_ws_loop(stop))
        except KeyboardInterrupt:
            raise
        return 0

    async def _btc_ws_loop(self, stop: asyncio.Event) -> None:
        websockets = _import_websockets()
        url = btc_ws_url_from_bot(self.bot)
        reconnect_backoff_seconds = 1.0
        while not stop.is_set():
            try:
                async with websockets.connect(url) as ws:
                    reconnect_backoff_seconds = 1.0
                    if self.bot.config.market_data.provider.lower() == "coinbase":
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "subscribe",
                                    "product_ids": [self.bot.config.market_data.product_id],
                                    "channels": ["ticker"],
                                }
                            )
                        )
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except TimeoutError:
                            continue
                        data = json.loads(raw)
                        try:
                            tick = (
                                parse_binance_book_ticker(data)
                                if self.bot.config.market_data.provider.lower() == "binance"
                                else parse_coinbase_ticker(data)
                            )
                        except ValueError:
                            continue
                        self.apply_btc_tick(tick)
                        if self._max_events_reached():
                            stop.set()
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - BTC websocket drops are transient; keep dashboard alive.
                if stop.is_set():
                    break
                self._emit_reconnect_warning(
                    warning="btc_ws_reconnect_failed",
                    error=exc,
                    retry_in_seconds=reconnect_backoff_seconds,
                )
                await asyncio.sleep(reconnect_backoff_seconds)
                reconnect_backoff_seconds = min(reconnect_backoff_seconds * 2.0, 30.0)

    async def _kalshi_ws_loop(self, stop: asyncio.Event) -> None:
        websockets = _import_websockets()
        rest_base_url = self._kalshi_rest_url_for_ws()
        url = kalshi_ws_url_from_rest_url(rest_base_url)
        reconnect_backoff_seconds = 1.0
        while not stop.is_set():
            if self.market is None:
                self.refresh_market()
            if self.market is None:
                await asyncio.sleep(1.0)
                continue
            subscribed_ticker = self.market.ticker
            try:
                headers = _kalshi_ws_auth_headers(
                    rest_base_url,
                    self.bot.config.market_data.request_timeout_seconds,
                )
                connect = _websockets_connect(websockets, url, headers=headers)
                async with connect as ws:
                    reconnect_backoff_seconds = 1.0
                    await ws.send(
                        json.dumps(
                            {
                                "id": 1,
                                "cmd": "subscribe",
                                "params": {
                                    "channels": ["orderbook_delta"],
                                    "market_tickers": [subscribed_ticker],
                                },
                            }
                        )
                    )
                    while not stop.is_set():
                        if self.market is not None and self.market.ticker != subscribed_ticker:
                            break
                        if self.refresh_market_if_closed(now=self.clock()):
                            if self.market is not None and self.market.ticker != subscribed_ticker:
                                break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except TimeoutError:
                            continue
                        data = json.loads(raw)
                        self.apply_kalshi_message(data)
                        if self._max_events_reached():
                            stop.set()
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - websocket loops must reconnect after transient/auth failures.
                if stop.is_set():
                    break
                warning = (
                    "kalshi_ws_auth_reconnect_failed"
                    if _is_kalshi_auth_error(exc)
                    else "kalshi_ws_reconnect_failed"
                )
                self._emit_reconnect_warning(
                    warning=warning,
                    error=exc,
                    retry_in_seconds=reconnect_backoff_seconds,
                )
                await asyncio.sleep(reconnect_backoff_seconds)
                reconnect_backoff_seconds = min(reconnect_backoff_seconds * 2.0, 30.0)

    def _emit_reconnect_warning(self, *, warning: str, error: Exception, retry_in_seconds: float) -> None:
        if warning.startswith("btc_"):
            self._btc_stream_warning = warning
        elif warning.startswith("kalshi_"):
            self._kalshi_stream_warning = warning
        if self.json_output:
            self.emit(
                dump_json(
                    {
                        "warning": warning,
                        "error": _fmt_error(error),
                        "retry_in_seconds": retry_in_seconds,
                    }
                )
            )
        else:
            self.emit(
                f"warning={warning} error={_fmt_error(error)} "
                f"retry_in={retry_in_seconds:.0f}s"
            )
        self.emit_state(force=True)

    def _kalshi_rest_url_for_ws(self) -> str:
        if self.bot.config.is_live_mode:
            return self.bot.config.live.base_url
        return self.bot.config.kalshi.base_url

    def _btc_product_label(self) -> str:
        if self.bot.config.market_data.provider.lower() == "binance":
            return self.bot.config.market_data.symbol
        return self.bot.config.market_data.product_id

    def _max_events_reached(self) -> bool:
        return self.max_events is not None and self._emitted >= self.max_events


async def run_realtime_state_stream(
    bot: KalshiBTC15MBot,
    *,
    json_output: bool = False,
    max_events: int | None = None,
    emit_min_interval_seconds: float = 1.0,
) -> int:
    streamer = RealtimeStateStreamer(
        bot,
        json_output=json_output,
        max_events=max_events,
        emit_min_interval_seconds=emit_min_interval_seconds,
    )
    return await streamer.run()


async def run_realtime_paper_stream(
    bot: KalshiBTC15MBot,
    *,
    json_output: bool = False,
    max_events: int | None = None,
    emit_min_interval_seconds: float = 1.0,
) -> int:
    streamer = RealtimeStateStreamer(
        bot,
        json_output=json_output,
        max_events=max_events,
        emit_min_interval_seconds=emit_min_interval_seconds,
        paper_trading=True,
    )
    return await streamer.run()


def _rest_orderbook(kalshi: KalshiPublicClient, ticker: str) -> KalshiOrderBook:
    payload = kalshi.get_orderbook(ticker, depth=100)
    raw = payload.get("orderbook_fp") or payload.get("orderbook") or payload
    if not isinstance(raw, Mapping):
        raw = {}
    return KalshiOrderBook.from_snapshot(ticker, raw)


def _kalshi_ws_auth_headers(rest_base_url: str, timeout_seconds: int) -> dict[str, str]:
    api_key_id = os.getenv("KALSHI_API_KEY_ID")
    private_key_file = os.getenv("KALSHI_PRIVATE_KEY_FILE")
    if not api_key_id or not private_key_file:
        raise KalshiCredentialError(
            "Kalshi WebSocket requires KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_FILE."
        )
    client = KalshiAuthenticatedClient(
        base_url=rest_base_url,
        api_key_id=api_key_id,
        private_key_file=private_key_file,
        timeout_seconds=timeout_seconds,
    )
    return client.websocket_auth_headers()


def _import_websockets():
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - exercised by operator environment.
        raise RuntimeError(
            "The 'websockets' package is required for stream-state. "
            "Run: pip install -e '.[dev]'"
        ) from exc
    return websockets


def _websockets_connect(websockets_module, url: str, *, headers: dict[str, str]):
    try:
        return websockets_module.connect(url, additional_headers=headers)
    except TypeError:  # websockets<14 used extra_headers.
        return websockets_module.connect(url, extra_headers=list(headers.items()))


def _levels_to_dict(levels: Any) -> dict[float, float]:
    result: dict[float, float] = {}
    if not isinstance(levels, list):
        return result
    for level in levels:
        if not isinstance(level, list | tuple) or len(level) < 2:
            continue
        price = _as_float(level[0], default=0.0)
        count = _as_float(level[1], default=0.0)
        if 0.0 < price < 1.0 and count > 0.0:
            result[price] = count
    return result


def _book_levels(msg: Mapping[str, Any], side: str) -> Any:
    return (
        msg.get(f"{side}_dollars_fp")
        or msg.get(f"{side}_dollars")
        or msg.get(f"{side}")
        or []
    )


def _message_time(msg: Mapping[str, Any]) -> datetime | None:
    raw = msg.get("time") or msg.get("ts")
    if isinstance(raw, str):
        return parse_ts(raw)
    ts_ms = _as_float(msg.get("ts_ms"), default=0.0)
    if ts_ms > 0:
        return datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
    return None


def _combined_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, Mapping) else payload


def _best_side(*, edge_yes: float | None, edge_no: float | None) -> tuple[str | None, float | None]:
    if edge_yes is None and edge_no is None:
        return None, None
    if edge_no is None or (edge_yes is not None and edge_yes >= edge_no):
        return "YES", edge_yes
    return "NO", edge_no


def _as_float(value: Any, *, default: float) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_kalshi_auth_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status_code == 401:
        return True
    text = str(exc).lower()
    return "401" in text or "unauthorized" in text


def _rollover_refresh_cadence(seconds_to_close: float | None) -> float:
    if seconds_to_close is not None and seconds_to_close <= 0:
        return ROLLOVER_RETRY_AFTER_CLOSE_SECONDS
    return ROLLOVER_RETRY_BEFORE_CLOSE_SECONDS


def _is_rate_limit_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status_code == 429:
        return True
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "too many requests" in text


def _fmt_error(value: Any) -> str:
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    if len(text) > 200:
        return text[:197] + "..."
    return text or "unknown"


def _seconds_label(value: Any) -> str:
    try:
        return f"{float(value):.0f}s"
    except (TypeError, ValueError):
        return "unknown"


def _fmt_edge(value: Any) -> str:
    try:
        return f"{float(value):+.3f}"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_dollars(value: Any) -> str:
    try:
        return f"{float(value):+.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:+.3f}%"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_ev_pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:+.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_signed_money(value: Any) -> str:
    try:
        dollars = float(value)
    except (TypeError, ValueError):
        return "n/a"
    sign = "+" if dollars >= 0 else "-"
    return f"{sign}${abs(dollars):.2f}"


def _fmt_probability(value: Any) -> str:
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "n/a"
