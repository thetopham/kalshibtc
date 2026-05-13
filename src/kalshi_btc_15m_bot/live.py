from __future__ import annotations

import base64
import json
import math
import os
import sqlite3
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .config import BotConfig, LiveConfig
from .ledger import parse_iso
from .models import KalshiMarket, Prediction, now_utc


class KalshiCredentialError(RuntimeError):
    """Raised when live Kalshi credentials are missing or unsafe."""


class LiveOrderError(RuntimeError):
    """Raised when a live order request fails after the local intent is recorded."""


class KalshiAuthenticatedClient:
    """Authenticated Kalshi REST client for guarded live operations."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key_id: str,
        private_key_file: str | Path,
        timeout_seconds: int = 20,
        timestamp_ms: Callable[[], str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key_id = api_key_id
        self.private_key_file = Path(private_key_file).expanduser()
        self.timeout_seconds = timeout_seconds
        self.timestamp_ms = timestamp_ms or (lambda: str(int(time.time() * 1000)))
        self.private_key = self._load_private_key(self.private_key_file)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "kalshi-btc-15m-bot/0.2"})

    @classmethod
    def from_env(cls, config: BotConfig) -> KalshiAuthenticatedClient:
        api_key_id = os.getenv("KALSHI_API_KEY_ID")
        private_key_file = os.getenv("KALSHI_PRIVATE_KEY_FILE")
        if not api_key_id:
            raise KalshiCredentialError("KALSHI_API_KEY_ID is required for live Kalshi requests.")
        if not private_key_file:
            raise KalshiCredentialError("KALSHI_PRIVATE_KEY_FILE is required for live Kalshi requests.")
        return cls(
            base_url=config.live.base_url,
            api_key_id=api_key_id,
            private_key_file=private_key_file,
            timeout_seconds=config.market_data.request_timeout_seconds,
        )

    def _load_private_key(self, path: Path):
        if not path.exists():
            raise KalshiCredentialError(f"Kalshi private key file not found: {path}")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise KalshiCredentialError(
                f"Kalshi private key file must be private; run: chmod 600 {path}"
            )
        with path.open("rb") as fh:
            return serialization.load_pem_private_key(
                fh.read(), password=None, backend=default_backend()
            )

    def auth_headers(self, method: str, path: str) -> dict[str, str]:
        timestamp = self.timestamp_ms()
        sign_path = self._signature_path(path)
        message = f"{timestamp}{method.upper()}{sign_path}".encode()
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        }

    def _signature_path(self, path: str) -> str:
        api_root = urlparse(self.base_url).path.rstrip("/")
        path_without_query = path.split("?", 1)[0]
        if not path_without_query.startswith("/"):
            path_without_query = f"/{path_without_query}"
        return f"{api_root}{path_without_query}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = self.auth_headers(method, path)
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            params=params,
            json=json_body,
            headers=headers,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def get_balance(self, *, subaccount: int = 0) -> dict[str, Any]:
        params = {"subaccount": subaccount} if subaccount else None
        return self._request("GET", "/portfolio/balance", params=params)

    def list_positions(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        count_filter: str | None = "position",
        ticker: str | None = None,
        event_ticker: str | None = None,
        subaccount: int = 0,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if count_filter:
            params["count_filter"] = count_filter
        if ticker:
            params["ticker"] = ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if subaccount:
            params["subaccount"] = subaccount
        payload = self._request("GET", "/portfolio/positions", params=params)
        return list(payload.get("market_positions", []))

    def list_fills(
        self,
        *,
        limit: int = 100,
        ticker: str | None = None,
        order_id: str | None = None,
        min_ts: int | None = None,
        subaccount: int = 0,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if order_id:
            params["order_id"] = order_id
        if min_ts is not None:
            params["min_ts"] = min_ts
        if subaccount:
            params["subaccount"] = subaccount
        payload = self._request("GET", "/portfolio/fills", params=params)
        return list(payload.get("fills", []))

    def create_order(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/portfolio/orders", json_body=body)


class LiveLedger:
    """SQLite audit ledger for live order intents, responses, and confirmed fills."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS live_orders (
                    client_order_id TEXT PRIMARY KEY,
                    prediction_id TEXT,
                    created_at TEXT NOT NULL,
                    market_ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    action TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    limit_price_cents INTEGER NOT NULL,
                    max_cost_cents INTEGER,
                    time_in_force TEXT NOT NULL,
                    status TEXT NOT NULL,
                    order_id TEXT,
                    request_json TEXT NOT NULL,
                    response_json TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS live_fills (
                    fill_id TEXT PRIMARY KEY,
                    order_id TEXT,
                    trade_id TEXT,
                    created_at TEXT NOT NULL,
                    market_ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    action TEXT NOT NULL,
                    count REAL NOT NULL,
                    price REAL NOT NULL,
                    fee_dollars REAL NOT NULL DEFAULT 0,
                    raw_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_live_orders_created ON live_orders(created_at);
                CREATE INDEX IF NOT EXISTS idx_live_orders_market ON live_orders(market_ticker, side, action);
                CREATE INDEX IF NOT EXISTS idx_live_fills_market ON live_fills(market_ticker, side, created_at);
                """
            )

    def record_order_intent(
        self,
        *,
        client_order_id: str,
        prediction_id: str | None,
        market_ticker: str,
        side: str,
        action: str,
        count: int,
        limit_price_cents: int,
        max_cost_cents: int | None,
        time_in_force: str,
        request: dict[str, Any],
        created_at: datetime | None = None,
    ) -> None:
        created = (created_at or now_utc()).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO live_orders (
                    client_order_id, prediction_id, created_at, market_ticker, side, action,
                    count, limit_price_cents, max_cost_cents, time_in_force, status,
                    request_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'intent_recorded', ?)
                """,
                (
                    client_order_id,
                    prediction_id,
                    created,
                    market_ticker,
                    side,
                    action,
                    count,
                    limit_price_cents,
                    max_cost_cents,
                    time_in_force,
                    _json(request),
                ),
            )

    def record_order_success(self, client_order_id: str, response: dict[str, Any]) -> None:
        order = response.get("order", response)
        status = str(order.get("status") or response.get("status") or "submitted")
        order_id = order.get("order_id") or order.get("id") or response.get("order_id")
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE live_orders
                SET status = ?, order_id = ?, response_json = ?, error = NULL
                WHERE client_order_id = ?
                """,
                (status, str(order_id) if order_id else None, _json(response), client_order_id),
            )

    def record_order_error(self, client_order_id: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE live_orders SET status = 'error', error = ? WHERE client_order_id = ?
                """,
                (error, client_order_id),
            )

    def record_fills(self, fills: list[Mapping[str, Any]]) -> int:
        written = 0
        with self.connect() as conn:
            for fill in fills:
                parsed = _parse_fill(fill)
                if not parsed:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO live_fills (
                        fill_id, order_id, trade_id, created_at, market_ticker, side,
                        action, count, price, fee_dollars, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        parsed["fill_id"],
                        parsed.get("order_id"),
                        parsed.get("trade_id"),
                        parsed["created_at"],
                        parsed["market_ticker"],
                        parsed["side"],
                        parsed["action"],
                        parsed["count"],
                        parsed["price"],
                        parsed["fee_dollars"],
                        _json(dict(fill)),
                    ),
                )
                written += 1
        return written

    def latest_orders(self, limit: int = 10) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM live_orders ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            )

    def latest_fills(self, limit: int = 10) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM live_fills ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            )

    def daily_order_count(self, at: datetime) -> int:
        day_prefix = at.date().isoformat()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM live_orders WHERE substr(created_at, 1, 10) = ?",
                (day_prefix,),
            ).fetchone()
        return int(row["n"] if row else 0)

    def recent_order_exists(
        self,
        *,
        market_ticker: str,
        side: str,
        action: str,
        now: datetime,
        within_seconds: int,
    ) -> bool:
        cutoff = (now - timedelta(seconds=within_seconds)).isoformat()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n
                FROM live_orders
                WHERE market_ticker = ? AND side = ? AND action = ? AND created_at >= ?
                """,
                (market_ticker, side, action, cutoff),
            ).fetchone()
        return bool(row and row["n"])

    def position_summaries(self) -> list[dict[str, Any]]:
        positions = []
        for state in self._position_states():
            if state["count"] > 1e-9:
                state["avg_entry_price"] = state["cost_basis"] / state["count"]
                positions.append(state)
        return positions

    def realized_pnl(self) -> float:
        return sum(float(state.get("realized_pnl", 0.0)) for state in self._position_states())

    def daily_realized_pnl(self, at: datetime) -> float:
        return sum(
            float(state.get("realized_pnl", 0.0))
            for state in self._position_states(day_prefix=at.date().isoformat())
        )

    def _position_states(self, *, day_prefix: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if day_prefix:
                rows = conn.execute(
                    """
                    SELECT * FROM live_fills
                    WHERE substr(created_at, 1, 10) = ?
                    ORDER BY created_at ASC, fill_id ASC
                    """,
                    (day_prefix,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM live_fills ORDER BY created_at ASC, fill_id ASC"
                ).fetchall()
        states: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (str(row["market_ticker"]), str(row["side"]).upper())
            state = states.setdefault(
                key,
                {
                    "market_ticker": key[0],
                    "side": key[1],
                    "count": 0.0,
                    "cost_basis": 0.0,
                    "realized_pnl": 0.0,
                    "fees_paid": 0.0,
                    "last_fill_at": None,
                },
            )
            count = float(row["count"])
            price = float(row["price"])
            fee = float(row["fee_dollars"] or 0.0)
            action = str(row["action"]).lower()
            state["fees_paid"] += fee
            state["last_fill_at"] = row["created_at"]
            if action == "buy":
                state["count"] += count
                state["cost_basis"] += count * price + fee
            elif action == "sell":
                if state["count"] <= 0:
                    state["realized_pnl"] += count * price - fee
                    continue
                sell_count = min(count, state["count"])
                avg_cost = state["cost_basis"] / state["count"] if state["count"] else 0.0
                state["realized_pnl"] += sell_count * (price - avg_cost) - fee
                state["count"] -= sell_count
                state["cost_basis"] -= avg_cost * sell_count
        return list(states.values())


@dataclass(frozen=True)
class OrderPlan:
    client_order_id: str
    market_ticker: str
    side: str
    action: str
    count: int
    limit_price_cents: int
    max_cost_cents: int | None
    time_in_force: str
    body: dict[str, Any]


class LiveTrader:
    """Guarded live trading adapter with explicit risk gates and audit ledger."""

    def __init__(
        self,
        config: BotConfig,
        *,
        client: KalshiAuthenticatedClient | Any | None = None,
        ledger: LiveLedger | None = None,
    ) -> None:
        if not config.is_live_mode:
            raise ValueError("LiveTrader requires trading_mode='live' and enable_live_orders=true.")
        self.config = config
        self.live_config = config.live
        self.client = client or KalshiAuthenticatedClient.from_env(config)
        self.ledger = ledger or LiveLedger(config.ledger_path)

    def auth_check(self) -> dict[str, Any]:
        balance = self.client.get_balance(subaccount=self.live_config.subaccount)
        positions = self.client.list_positions(
            limit=100,
            count_filter="position",
            subaccount=self.live_config.subaccount,
        )
        return {
            "environment": self.live_config.environment,
            "base_url": self.live_config.base_url,
            "balance_dollars": cents_to_dollars(balance.get("balance")),
            "portfolio_value_dollars": cents_to_dollars(balance.get("portfolio_value")),
            "updated_ts": balance.get("updated_ts"),
            "nonzero_positions": len(positions),
            "orders_submitted": False,
            "boundary": "authenticated read-only check; no orders submitted",
        }

    def sync_recent_fills(self, *, ticker: str | None = None) -> int:
        fills = self.client.list_fills(
            limit=100,
            ticker=ticker,
            subaccount=self.live_config.subaccount,
        )
        return self.ledger.record_fills(fills)

    def maybe_submit_entry(self, prediction: Prediction) -> dict[str, Any]:
        now = prediction.created_at
        reason = self.live_entry_skip_reason(prediction, now=now)
        if reason:
            return {"submitted": False, "reason": reason, "mode": "live"}
        plan = self._entry_order_plan(prediction)
        return self._submit_order(plan, prediction_id=prediction.prediction_id)

    def live_entry_skip_reason(self, prediction: Prediction, *, now: datetime | None = None) -> str | None:
        now = now or now_utc()
        if not self.live_config.auto_trade:
            return "live_auto_trade_disabled"
        if not prediction.action.startswith("BUY_"):
            return "action_not_buy"
        if prediction.side not in {"YES", "NO"}:
            return "invalid_side"
        if self.ledger.daily_order_count(now) >= self.live_config.max_daily_orders:
            return f"max_daily_orders: reached {self.live_config.max_daily_orders}"
        if len(self.ledger.position_summaries()) >= self.live_config.max_open_positions:
            return f"max_open_positions: reached {self.live_config.max_open_positions}"
        if self._has_position(prediction.market.ticker, prediction.side):
            return "position_already_open_for_market_side"
        if self.ledger.recent_order_exists(
            market_ticker=prediction.market.ticker,
            side=prediction.side,
            action="buy",
            now=now,
            within_seconds=self.live_config.min_seconds_between_orders,
        ):
            return "recent_live_buy_order_exists"
        if prediction.market.liquidity < self.live_config.min_liquidity_dollars:
            return (
                "min_liquidity: "
                f"{prediction.market.liquidity:.2f} below {self.live_config.min_liquidity_dollars:.2f}"
            )
        entry_price = prediction.market.yes_ask if prediction.side == "YES" else prediction.market.no_ask
        exit_bid = prediction.market.yes_bid if prediction.side == "YES" else prediction.market.no_bid
        if not (0.0 < entry_price < 1.0):
            return "invalid_entry_price"
        spread = entry_price - exit_bid
        if spread > self.live_config.max_spread:
            return f"max_spread: side spread {spread:.3f} exceeds {self.live_config.max_spread:.3f}"
        count = self._entry_contract_count(prediction)
        if count < 1:
            return "live_contract_count_below_one"
        balance = self.client.get_balance(subaccount=self.live_config.subaccount)
        balance_dollars = cents_to_dollars(balance.get("balance"))
        max_cost = (count * price_to_cents(entry_price)) / 100
        if balance_dollars - max_cost < self.live_config.min_cash_reserve_dollars:
            return (
                "min_cash_reserve: "
                f"balance ${balance_dollars:.2f} - order ${max_cost:.2f} < reserve ${self.live_config.min_cash_reserve_dollars:.2f}"
            )
        if self.ledger.daily_realized_pnl(now) <= -abs(self.live_config.max_daily_loss_dollars):
            return "max_daily_loss_reached"
        return None

    def manage_open_positions(
        self,
        get_market: Callable[[str], KalshiMarket],
        *,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        now = now or now_utc()
        self.sync_recent_fills()
        managed: list[dict[str, Any]] = []
        for position in self.ledger.position_summaries():
            market = get_market(position["market_ticker"])
            mark = mark_live_position_to_market(position, market)
            signal = evaluate_live_exit(mark, self.live_config, now=now)
            mark["exit_signal"] = signal or "hold"
            if signal:
                if self.ledger.recent_order_exists(
                    market_ticker=position["market_ticker"],
                    side=position["side"],
                    action="sell",
                    now=now,
                    within_seconds=self.live_config.min_seconds_between_orders,
                ):
                    mark["submitted"] = False
                    mark["reason"] = "recent_live_sell_order_exists"
                else:
                    plan = self._exit_order_plan(position, market, signal)
                    result = self._submit_order(plan, prediction_id=None)
                    mark.update(result)
            else:
                mark["submitted"] = False
            managed.append(mark)
        return managed

    def live_status(self, *, sync_fills: bool = False) -> dict[str, Any]:
        synced = self.sync_recent_fills() if sync_fills else 0
        positions = self.ledger.position_summaries()
        orders = [row_to_dict(row) for row in self.ledger.latest_orders(limit=10)]
        fills = [row_to_dict(row) for row in self.ledger.latest_fills(limit=10)]
        return {
            "environment": self.live_config.environment,
            "base_url": self.live_config.base_url,
            "synced_fills": synced,
            "open_positions": positions,
            "latest_orders": orders,
            "latest_fills": fills,
            "realized_pnl": self.ledger.realized_pnl(),
            "boundary": "LIVE TRADING ENABLED by config; order submission remains cap-gated and audited",
        }

    def _has_position(self, market_ticker: str, side: str) -> bool:
        return any(
            pos["market_ticker"] == market_ticker and pos["side"] == side
            for pos in self.ledger.position_summaries()
        )

    def _entry_contract_count(self, prediction: Prediction) -> int:
        entry_price = prediction.market.yes_ask if prediction.side == "YES" else prediction.market.no_ask
        if entry_price <= 0:
            return 0
        raw_count = math.floor(min(prediction.stake_dollars, self.live_config.max_order_dollars) / entry_price)
        return max(0, min(self.live_config.max_contracts, raw_count))

    def _entry_order_plan(self, prediction: Prediction) -> OrderPlan:
        side = str(prediction.side)
        entry_price = prediction.market.yes_ask if side == "YES" else prediction.market.no_ask
        price_cents = price_to_cents(entry_price)
        count = self._entry_contract_count(prediction)
        client_order_id = f"kbtc15-{prediction.prediction_id[:12]}-buy-{side.lower()}"
        body: dict[str, Any] = {
            "ticker": prediction.market.ticker,
            "side": side.lower(),
            "action": "buy",
            "client_order_id": client_order_id,
            "count": count,
            "time_in_force": self.live_config.time_in_force,
            "post_only": False,
            "cancel_order_on_pause": True,
        }
        if side == "YES":
            body["yes_price"] = price_cents
        else:
            body["no_price"] = price_cents
        max_cost_cents = count * price_cents
        body["buy_max_cost"] = max_cost_cents
        return OrderPlan(
            client_order_id=client_order_id,
            market_ticker=prediction.market.ticker,
            side=side,
            action="buy",
            count=count,
            limit_price_cents=price_cents,
            max_cost_cents=max_cost_cents,
            time_in_force=self.live_config.time_in_force,
            body=body,
        )

    def _exit_order_plan(
        self,
        position: Mapping[str, Any],
        market: KalshiMarket,
        exit_reason: str,
    ) -> OrderPlan:
        side = str(position["side"])
        price = market.yes_bid if side == "YES" else market.no_bid
        price_cents = price_to_cents(price)
        count = math.floor(float(position["count"]))
        client_order_id = (
            f"kbtc15-{position['market_ticker']}-{exit_reason}-sell-{side.lower()}"[:64]
        )
        body: dict[str, Any] = {
            "ticker": position["market_ticker"],
            "side": side.lower(),
            "action": "sell",
            "client_order_id": client_order_id,
            "count": count,
            "time_in_force": self.live_config.time_in_force,
            "reduce_only": True,
            "post_only": False,
            "cancel_order_on_pause": True,
        }
        if side == "YES":
            body["yes_price"] = price_cents
        else:
            body["no_price"] = price_cents
        return OrderPlan(
            client_order_id=client_order_id,
            market_ticker=str(position["market_ticker"]),
            side=side,
            action="sell",
            count=count,
            limit_price_cents=price_cents,
            max_cost_cents=None,
            time_in_force=self.live_config.time_in_force,
            body=body,
        )

    def _submit_order(self, plan: OrderPlan, *, prediction_id: str | None) -> dict[str, Any]:
        if plan.count < 1:
            return {"submitted": False, "reason": "live_contract_count_below_one", "mode": "live"}
        self.ledger.record_order_intent(
            client_order_id=plan.client_order_id,
            prediction_id=prediction_id,
            market_ticker=plan.market_ticker,
            side=plan.side,
            action=plan.action,
            count=plan.count,
            limit_price_cents=plan.limit_price_cents,
            max_cost_cents=plan.max_cost_cents,
            time_in_force=plan.time_in_force,
            request=plan.body,
        )
        try:
            response = self.client.create_order(plan.body)
        except Exception as exc:  # noqa: BLE001 - record audit trail before surfacing failure.
            self.ledger.record_order_error(plan.client_order_id, str(exc))
            raise LiveOrderError(str(exc)) from exc
        self.ledger.record_order_success(plan.client_order_id, response)
        return {
            "submitted": True,
            "client_order_id": plan.client_order_id,
            "market_ticker": plan.market_ticker,
            "side": plan.side,
            "action": plan.action,
            "count": plan.count,
            "limit_price_cents": plan.limit_price_cents,
            "mode": "live",
            "response_status": _response_status(response),
        }


def mark_live_position_to_market(position: Mapping[str, Any], market: KalshiMarket) -> dict[str, Any]:
    side = str(position["side"])
    if side == "YES":
        mark_price = market.yes_bid or market.yes_mid
        price_source = "yes_bid" if market.yes_bid else "yes_mid_fallback"
    elif side == "NO":
        mark_price = market.no_bid or market.no_mid
        price_source = "no_bid" if market.no_bid else "no_mid_fallback"
    else:
        raise ValueError(f"Unsupported live position side: {side}")
    count = float(position["count"])
    avg_entry = float(position["avg_entry_price"])
    cost_basis = float(position["cost_basis"])
    current_value = count * mark_price
    unrealized_pnl = current_value - cost_basis
    return {
        **dict(position),
        "mark_price": mark_price,
        "price_source": price_source,
        "current_value": current_value,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pnl_pct": (unrealized_pnl / cost_basis) if cost_basis else 0.0,
        "avg_entry_price": avg_entry,
        "market_close_time": market.close_time.isoformat() if market.close_time else None,
        "liquidity": market.liquidity,
    }


def evaluate_live_exit(
    mark: Mapping[str, Any],
    live_config: LiveConfig,
    *,
    now: datetime | None = None,
) -> str | None:
    now = now or now_utc()
    pnl_pct = float(mark.get("unrealized_pnl_pct", 0.0))
    if pnl_pct >= live_config.take_profit_pct:
        return "take_profit"
    if pnl_pct <= live_config.stop_loss_pct:
        return "stop_loss"
    close_time = parse_iso(str(mark.get("market_close_time") or ""))
    if close_time is not None:
        seconds_to_close = (close_time - now).total_seconds()
        if seconds_to_close <= live_config.force_close_seconds_to_close:
            return "time_exit"
    return None


def price_to_cents(price: float) -> int:
    return max(1, min(99, int(round(price * 100))))


def cents_to_dollars(value: Any) -> float:
    try:
        return float(value) / 100
    except (TypeError, ValueError):
        return 0.0


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _parse_fill(fill: Mapping[str, Any]) -> dict[str, Any] | None:
    fill_id = str(fill.get("fill_id") or "")
    market_ticker = str(fill.get("market_ticker") or fill.get("ticker") or "")
    side = str(fill.get("side") or fill.get("outcome_side") or "").upper()
    action = str(fill.get("action") or "").lower()
    if not fill_id or not market_ticker or side not in {"YES", "NO"} or action not in {"buy", "sell"}:
        return None
    count = _float(fill.get("count_fp") or fill.get("count"), default=0.0)
    if count <= 0:
        return None
    price_key = "yes_price_dollars" if side == "YES" else "no_price_dollars"
    price = _float(fill.get(price_key), default=0.0)
    if price <= 0:
        price_cents_key = "yes_price" if side == "YES" else "no_price"
        price = _float(fill.get(price_cents_key), default=0.0) / 100
    if price <= 0:
        return None
    created = str(fill.get("created_time") or fill.get("created_at") or now_utc().isoformat())
    parsed_created = parse_iso(created)
    return {
        "fill_id": fill_id,
        "order_id": fill.get("order_id"),
        "trade_id": fill.get("trade_id"),
        "created_at": (parsed_created or now_utc()).isoformat(),
        "market_ticker": market_ticker,
        "side": side,
        "action": action,
        "count": count,
        "price": price,
        "fee_dollars": _float(fill.get("fee_cost") or fill.get("fee_cost_dollars"), default=0.0),
    }


def _float(value: Any, *, default: float) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _response_status(response: Mapping[str, Any]) -> str:
    order = response.get("order")
    if isinstance(order, Mapping):
        return str(order.get("status") or "submitted")
    return str(response.get("status") or "submitted")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)
