from __future__ import annotations

import json
from typing import Any

from .config import BotConfig
from .kalshi_client import KalshiPublicClient
from .ledger import PaperLedger, evaluate_paper_exit, mark_open_trade_to_market, row_to_dict
from .live import LiveLedger, LiveOrderError, LiveTrader
from .market_data import candles_to_frame, provider_from_config
from .models import now_utc
from .predictor import BTC15MPredictor


class KalshiBTC15MBot:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.kalshi = KalshiPublicClient(
            base_url=config.kalshi.base_url,
            timeout_seconds=config.market_data.request_timeout_seconds,
        )
        self.market_data = provider_from_config(config.market_data)
        self.predictor = BTC15MPredictor(config)
        self.ledger = PaperLedger(config.ledger_path, config.paper)
        self.live_ledger = LiveLedger(config.ledger_path)
        self.live_trader: LiveTrader | None = None
        if config.is_live_mode:
            self.live_trader = LiveTrader(config, ledger=self.live_ledger)

    def scan_once(self) -> dict[str, Any]:
        settled = self.resolve_open_trades()
        managed_positions = self.manage_open_positions()
        managed_live_positions = self.manage_live_positions() if self.config.is_live_mode else []
        market = self.kalshi.current_btc15m_market(
            self.config.kalshi.series_ticker, status=self.config.kalshi.market_status
        )
        candles = self.market_data.fetch_candles(self.config.market_data.lookback_days)
        frame = candles_to_frame(
            candles,
            granularity_seconds=self.config.market_data.granularity_seconds,
            drop_incomplete=True,
        )
        current_price = self.market_data.current_price()
        prediction = self.predictor.predict(
            frame,
            market=market,
            current_price=current_price,
            now=now_utc(),
        )
        self.ledger.record_prediction(prediction)
        live_order_result: dict[str, Any] | None = None
        if self.config.is_live_mode:
            trade_id = None
            paper_trade_skip_reason = "live_mode_uses_guarded_live_adapter"
            if any(row.get("error") for row in managed_live_positions):
                live_order_result = {
                    "submitted": False,
                    "reason": "live_position_manager_error_fail_closed",
                    "mode": "live",
                }
            elif self.live_trader is None:
                live_order_result = {"submitted": False, "reason": "live_trader_not_configured", "mode": "live"}
            else:
                try:
                    live_order_result = self.live_trader.maybe_submit_entry(prediction)
                    if live_order_result.get("submitted"):
                        live_order_result["synced_fills"] = self.live_trader.sync_recent_fills(
                            ticker=prediction.market.ticker
                        )
                except LiveOrderError as exc:
                    live_order_result = {
                        "submitted": False,
                        "reason": "order_submission_failed",
                        "error": str(exc),
                        "mode": "live",
                    }
                if not live_order_result.get("submitted"):
                    reason = live_order_result.get("reason", "unknown")
                    prediction.reasons.append(f"live_order_skipped: {reason}")
        else:
            trade_id = self.ledger.maybe_open_paper_trade(prediction)
            paper_trade_skip_reason = None
            if trade_id is None and prediction.action.startswith("BUY_"):
                paper_trade_skip_reason = self.ledger.paper_entry_skip_reason(prediction)
        account = self.ledger.account()
        payload = prediction.to_jsonable()
        if paper_trade_skip_reason:
            payload["reasons"].append(f"paper_trade_skipped: {paper_trade_skip_reason}")
        payload["paper_trade_id"] = trade_id
        payload["paper_trade_skip_reason"] = paper_trade_skip_reason
        payload["live_order"] = live_order_result
        payload["paper_account"] = account.__dict__
        if self.config.is_live_mode and self.live_trader is not None:
            try:
                payload["live_account"] = self.live_trader.account_snapshot()
            except Exception as exc:  # noqa: BLE001 - scan output should still show fail-closed state.
                payload["live_account"] = {"error": str(exc)}
        else:
            payload["live_account"] = None
        payload["position_manager"] = {
            "settled_trades": settled,
            "checked_open_positions": len(managed_positions),
            "paper_closed_positions": sum(1 for row in managed_positions if row.get("paper_closed")),
            "checked_live_positions": len(managed_live_positions),
            "live_exit_orders_submitted": sum(1 for row in managed_live_positions if row.get("submitted")),
        }
        payload["managed_live_positions"] = managed_live_positions
        payload["safety"] = self._safety_payload()
        return payload

    def resolve_open_trades(self) -> int:
        settled = 0
        for row in self.ledger.open_trades():
            market = self.kalshi.get_market(row["market_ticker"])
            settled += self.ledger.settle_market(market)
        return settled

    def manage_open_positions(self) -> list[dict[str, Any]]:
        now = now_utc()
        managed: list[dict[str, Any]] = []
        for row in self.ledger.open_trades():
            try:
                market = self.kalshi.get_market(row["market_ticker"])
                mark = mark_open_trade_to_market(row, market)
                exit_signal = evaluate_paper_exit(row, mark, self.config.paper, now=now)
                mark["exit_signal"] = exit_signal or "hold"
                if exit_signal:
                    mark["paper_closed"] = bool(
                        self.ledger.close_paper_trade(
                            row["id"],
                            exit_price=float(mark["mark_price"]),
                            exit_reason=exit_signal,
                            closed_at=now,
                        )
                    )
                else:
                    mark["paper_closed"] = False
                managed.append(mark)
            except Exception as exc:  # noqa: BLE001 - management should not hide ledger status.
                data = row_to_dict(row)
                data["quote_error"] = str(exc)
                data["exit_signal"] = "unknown"
                data["paper_closed"] = False
                managed.append(data)
        return managed

    def manage_live_positions(self) -> list[dict[str, Any]]:
        if self.live_trader is None:
            return []
        try:
            return self.live_trader.manage_open_positions(self.kalshi.get_market)
        except Exception as exc:  # noqa: BLE001 - live manager must fail closed and remain auditable.
            return [{"error": str(exc), "submitted": False, "exit_signal": "unknown"}]

    def status(self) -> dict[str, Any]:
        account = self.ledger.account()
        predictions = [row_to_dict(row) for row in self.ledger.latest_predictions(limit=5)]
        trades = [row_to_dict(row) for row in self.ledger.latest_trades(limit=10)]
        open_positions = self._open_position_marks()
        performance = self.ledger.performance_summary(open_positions)
        live_status = self.live_status(sync_fills=False)
        return {
            "as_of": now_utc().isoformat(),
            "ledger_path": str(self.config.ledger_path),
            "paper_account": account.__dict__,
            "performance": performance,
            "latest_predictions": predictions,
            "latest_trades": trades,
            "open_positions": open_positions,
            "live": live_status,
            "latest_live_orders": live_status.get("latest_orders", []),
            "latest_live_fills": live_status.get("latest_fills", []),
            "safety": self._safety_payload(),
        }

    def report(self) -> dict[str, Any]:
        return self.status()

    def live_auth_check(self) -> dict[str, Any]:
        if self.live_trader is None:
            return {
                "mode": self.config.trading_mode,
                "environment": self.config.live.environment,
                "base_url": self.config.live.base_url,
                "enabled": False,
                "boundary": "live adapter disabled; no authenticated request made",
            }
        payload = self.live_trader.auth_check()
        payload["mode"] = self.config.trading_mode
        payload["enabled"] = True
        return payload

    def live_status(self, *, sync_fills: bool) -> dict[str, Any]:
        if self.live_trader is not None:
            return self.live_trader.live_status(sync_fills=sync_fills)
        return {
            "environment": self.config.live.environment,
            "base_url": self.config.live.base_url,
            "synced_fills": 0,
            "open_positions": self.live_ledger.position_summaries(bot_owned_only=True),
            "latest_orders": [row_to_dict(row) for row in self.live_ledger.latest_orders(limit=10)],
            "latest_fills": [
                row_to_dict(row) for row in self.live_ledger.latest_fills(limit=10, bot_owned_only=True)
            ],
            "realized_pnl": self.live_ledger.realized_pnl(bot_owned_only=True),
            "boundary": "live adapter disabled; no authenticated requests or orders",
        }

    def sync_live_fills(self) -> dict[str, Any]:
        if self.live_trader is None:
            return {
                "synced_fills": 0,
                "enabled": False,
                "boundary": "live adapter disabled; no authenticated request made",
            }
        synced = self.live_trader.sync_recent_fills()
        return {
            "synced_fills": synced,
            "enabled": True,
            "live_status": self.live_trader.live_status(sync_fills=False),
            "boundary": "authenticated fills sync only; no orders submitted",
        }

    def _open_position_marks(self) -> list[dict[str, Any]]:
        positions: list[dict[str, Any]] = []
        for row in self.ledger.open_trades():
            try:
                market = self.kalshi.get_market(row["market_ticker"])
                mark = mark_open_trade_to_market(row, market)
                mark["exit_signal"] = evaluate_paper_exit(row, mark, self.config.paper) or "hold"
                positions.append(mark)
            except Exception as exc:  # noqa: BLE001 - status must remain useful offline.
                data = row_to_dict(row)
                data["quote_error"] = str(exc)
                positions.append(data)
        return positions

    def markets(self) -> list[dict[str, Any]]:
        markets = self.kalshi.list_markets(
            series_ticker=self.config.kalshi.series_ticker,
            status=self.config.kalshi.market_status,
            limit=20,
        )
        return [market.to_jsonable() for market in markets]

    def _safety_payload(self) -> dict[str, Any]:
        return {
            "trading_mode": self.config.trading_mode,
            "enable_live_orders": self.config.enable_live_orders,
            "live_orders_supported": self.config.is_live_mode,
            "boundary": _safety_boundary(self.config),
        }


def format_scan(payload: dict[str, Any]) -> str:
    side = payload.get("side") or "NONE"
    mode = payload.get("safety", {}).get("trading_mode", "paper")
    lines = [
        f"BTC 15m Kalshi {mode} scan",
        f"market: {payload['market_ticker']} closes {payload.get('market_close_time')}",
        f"target: {payload.get('target_price')} current: {payload.get('current_price'):.2f}",
        f"prob_yes: {payload['probability_yes']:.3f} prob_no: {payload['probability_no']:.3f}",
        f"action: {payload['action']} side: {side} edge: {payload['edge']:.3f} stake: ${payload['stake_dollars']:.2f}",
    ]
    if payload.get("live_order") is not None:
        live = payload["live_order"]
        if live.get("submitted"):
            lines.append(
                "live_order: submitted "
                f"client_order_id={live.get('client_order_id')} count={live.get('count')} "
                f"price_cents={live.get('limit_price_cents')} fills_synced={live.get('synced_fills', 0)}"
            )
        else:
            lines.append(f"live_order: skipped reason={live.get('reason', 'unknown')}")
    else:
        lines.append(
            f"paper_trade_id: {payload.get('paper_trade_id') or 'none'}"
            + (f" skip={payload.get('paper_trade_skip_reason')}" if payload.get("paper_trade_skip_reason") else "")
        )
    account_lines: list[str]
    if mode == "live":
        live_account = payload.get("live_account") or {}
        if live_account.get("error"):
            account_lines = [f"live_account: unavailable error={live_account['error']}"]
        else:
            account_lines = [
                "live_balance: "
                f"${float(live_account.get('balance_dollars', 0.0)):.2f} "
                f"portfolio_value: ${float(live_account.get('portfolio_value_dollars', 0.0)):.2f} "
                f"remote_positions: {live_account.get('nonzero_positions', 0)}"
            ]
        account_lines.append("paper_ledger: isolated; not used for live sizing")
    else:
        account_lines = [
            f"cash: ${payload['paper_account']['cash']:.2f} open_notional: ${payload['paper_account']['open_notional']:.2f} realized_pnl: ${payload['paper_account']['realized_pnl']:.2f}"
        ]
    lines.extend(
        [
            *account_lines,
            f"boundary: {payload.get('safety', {}).get('boundary', 'unknown')}",
            "top reasons:",
        ]
    )
    for reason in payload.get("reasons", [])[:6]:
        lines.append(f"- {reason}")
    return "\n".join(lines)


def format_status(payload: dict[str, Any]) -> str:
    account = payload["paper_account"]
    safety = payload.get("safety", {})
    trading_mode = safety.get("trading_mode", "paper")
    boundary = _boundary_from_safety(safety)
    live = payload.get("live") or {}
    live_account = live.get("account") or {}
    lines = [
        f"BTC 15m Kalshi {trading_mode} bot status",
        f"ledger: {payload['ledger_path']}",
    ]
    if trading_mode == "live":
        if live_account.get("error"):
            lines.append(f"live_account: unavailable error={live_account['error']}")
        else:
            lines.append(
                "live_balance: "
                f"${float(live_account.get('balance_dollars', 0.0)):.2f} "
                f"portfolio_value: ${float(live_account.get('portfolio_value_dollars', 0.0)):.2f} "
                f"remote_positions: {live_account.get('nonzero_positions', 0)}"
            )
        lines.append(
            f"paper_ledger: cash=${account['cash']:.2f} open_notional=${account['open_notional']:.2f} "
            f"realized_pnl=${account['realized_pnl']:.2f} open_trades={account['open_trades']} "
            f"settled_trades={account['settled_trades']}"
        )
        lines.append(f"live mode: {boundary}")
    else:
        lines.extend(
            [
                f"cash: ${account['cash']:.2f} open_notional: ${account['open_notional']:.2f} realized_pnl: ${account['realized_pnl']:.2f}",
                f"open_trades: {account['open_trades']} settled_trades: {account['settled_trades']}",
                f"boundary: {boundary}",
            ]
        )
    if payload.get("performance"):
        perf = payload["performance"]
        perf_label = "paper_equity" if trading_mode == "live" else "equity"
        lines.append(
            f"{perf_label}: ${perf['total_equity']:.2f} open_unrealized: {_signed_money(perf['open_unrealized_pnl'])} "
            f"win_rate: {_pct(perf['win_rate'])} expectancy: {_signed_money(perf['expectancy_dollars'])}/trade"
        )
    live = payload.get("live") or {}
    if live and (trading_mode == "live" or live.get("open_positions") or live.get("latest_orders")):
        lines.append(
            f"live env={live.get('environment')} realized_pnl={_signed_money(float(live.get('realized_pnl', 0.0)))} "
            f"open_positions={len(live.get('open_positions', []))} synced_fills={live.get('synced_fills', 0)}"
        )
    if payload["latest_predictions"]:
        lines.append("latest predictions:")
        for row in payload["latest_predictions"][:3]:
            lines.append(
                f"- {row['created_at']} {row['market_ticker']} {row['action']} "
                f"p_yes={row['probability_yes']:.3f} edge={row['edge']:.3f}"
            )
    if payload.get("open_positions"):
        lines.append("open paper positions:")
        for row in payload["open_positions"][:5]:
            if row.get("quote_error"):
                lines.append(
                    f"- PAPER OPEN {row['id']} {row['market_ticker']} {row['side']} "
                    f"entry={row['entry_price']:.3f} notional=${row['notional']:.2f} "
                    f"quote_error={row['quote_error']}"
                )
                continue
            lines.append(
                f"- PAPER OPEN {row['trade_id']} {row['market_ticker']} {row['side']} "
                f"entry={row['entry_price']:.3f} mark={row['mark_price']:.3f} "
                f"unrealized={_signed_money(row['unrealized_pnl'])} "
                f"({_signed_pct(row['unrealized_pnl_pct'])}) max_win={_signed_money(row['max_profit_if_correct'])} "
                f"liquidity=${row['liquidity']:.2f} exit_signal={row.get('exit_signal', 'hold')} "
                f"closes={row.get('market_close_time')}"
            )
    if live.get("open_positions"):
        lines.append("open live positions:")
        for row in live["open_positions"][:5]:
            lines.append(
                f"- LIVE OPEN {row['market_ticker']} {row['side']} count={row['count']:.2f} "
                f"avg_entry={row['avg_entry_price']:.3f} realized={_signed_money(row.get('realized_pnl', 0.0))}"
            )
    if payload["latest_trades"]:
        lines.append("latest paper trades:")
        for row in payload["latest_trades"][:5]:
            pnl = row["realized_pnl"] if row["realized_pnl"] is not None else 0.0
            lines.append(
                f"- {row['created_at']} {row['market_ticker']} {row['side']} "
                f"{row['status']} notional=${row['notional']:.2f} pnl=${pnl:.2f}"
            )
    if payload.get("latest_live_orders"):
        lines.append("latest live orders:")
        for row in payload["latest_live_orders"][:5]:
            order_id = row.get("client_order_id") or row.get("id") or "unknown"
            notional = _order_notional_dollars(row)
            lines.append(
                f"- LIVE ORDER {order_id} {row.get('market_ticker')} {row.get('side')} {row.get('action')} "
                f"{row.get('status')} count={row.get('count')} notional=${notional:.2f}"
            )
    if payload.get("latest_live_fills"):
        lines.append("latest live fills:")
        for row in payload["latest_live_fills"][:5]:
            fill_id = row.get("fill_id") or "unknown"
            lines.append(
                f"- LIVE FILL {fill_id} {row.get('market_ticker')} {row.get('side')} {row.get('action')} "
                f"count={float(row.get('count', 0.0)):.2f} price={float(row.get('price', 0.0)):.3f}"
            )
    return "\n".join(lines)


def format_report(payload: dict[str, Any]) -> str:
    perf = payload["performance"]
    safety = payload.get("safety", {})
    mode = safety.get("trading_mode", "paper")
    equity_label = "paper_total_equity" if mode == "live" else "total_equity"
    lines = [
        "BTC 15m Kalshi operator report",
        f"ledger: {payload['ledger_path']}",
        f"{equity_label}: ${perf['total_equity']:.2f}",
        f"paper_realized_pnl: {_signed_money(perf['realized_pnl'])} open_unrealized: {_signed_money(perf['open_unrealized_pnl'])}",
        (
            f"trades: {perf['total_trades']} total / {perf['closed_trades']} closed / "
            f"{perf['open_trades']} open; win_rate: {_pct(perf['win_rate'])}"
        ),
        f"expectancy: {_signed_money(perf['expectancy_dollars'])}/trade roi_on_risk: {_signed_pct(perf['realized_roi_on_risk'])}",
        f"largest_win: {_signed_money(perf['largest_win'])} largest_loss: {_signed_money(perf['largest_loss'])}",
        f"boundary: {_boundary_from_safety(safety)}",
    ]
    live = payload.get("live") or {}
    if live:
        live_account = live.get("account") or {}
        if live_account.get("error"):
            lines.append(f"live_account: unavailable error={live_account['error']}")
        elif mode == "live":
            lines.append(
                "live_balance: "
                f"${float(live_account.get('balance_dollars', 0.0)):.2f} "
                f"portfolio_value: ${float(live_account.get('portfolio_value_dollars', 0.0)):.2f} "
                f"remote_positions: {live_account.get('nonzero_positions', 0)}"
            )
        lines.append(
            f"live: env={live.get('environment')} open_positions={len(live.get('open_positions', []))} "
            f"realized_pnl={_signed_money(float(live.get('realized_pnl', 0.0)))} latest_orders={len(live.get('latest_orders', []))}"
        )
    if payload.get("open_positions"):
        lines.append("open positions:")
        for row in payload["open_positions"][:10]:
            if row.get("quote_error"):
                lines.append(
                    f"- PAPER OPEN {row['id']} {row['market_ticker']} {row['side']} quote_error={row['quote_error']}"
                )
                continue
            lines.append(
                f"- PAPER OPEN {row['trade_id']} {row['market_ticker']} {row['side']} "
                f"entry={row['entry_price']:.3f} mark={row['mark_price']:.3f} "
                f"unrealized={_signed_money(row['unrealized_pnl'])} "
                f"({_signed_pct(row['unrealized_pnl_pct'])}) exit_signal={row.get('exit_signal', 'hold')} "
                f"closes={row.get('market_close_time')}"
            )
    return "\n".join(lines)


def format_live_auth_check(payload: dict[str, Any]) -> str:
    lines = [
        "Kalshi live auth check",
        f"mode: {payload.get('mode', 'unknown')}",
        f"environment: {payload.get('environment', 'unknown')}",
        f"base_url: {payload.get('base_url', 'unknown')}",
        f"boundary: {payload.get('boundary', 'unknown')}",
    ]
    if payload.get("enabled") or "balance_dollars" in payload:
        lines.extend(
            [
                f"balance: ${float(payload.get('balance_dollars', 0.0)):.2f}",
                f"portfolio_value: ${float(payload.get('portfolio_value_dollars', 0.0)):.2f}",
                f"open_positions: {payload.get('nonzero_positions', payload.get('open_position_count', 0))}",
            ]
        )
    return "\n".join(lines)


def _order_notional_dollars(row: dict[str, Any]) -> float:
    if row.get("notional_dollars") is not None:
        return float(row["notional_dollars"])
    if row.get("max_cost_cents") is not None:
        return float(row["max_cost_cents"] or 0) / 100.0
    if row.get("limit_price_cents") is not None and row.get("count") is not None:
        return float(row["limit_price_cents"] or 0) * float(row["count"] or 0) / 100.0
    return 0.0


def _boundary_from_safety(safety: dict[str, Any]) -> str:
    explicit = safety.get("boundary")
    if explicit:
        return str(explicit)
    live_supported = str(bool(safety.get("live_orders_supported", False))).lower()
    if safety.get("trading_mode") == "live" or safety.get("enable_live_orders"):
        return f"guarded live IOC limit orders enabled; live_orders_supported={live_supported}"
    return f"paper-only; live_orders_supported={live_supported}"


def _safety_boundary(config: BotConfig) -> str:
    if config.is_live_mode:
        return "guarded live IOC limit orders enabled; risk-capped, reduce-only exits, audited SQLite ledger"
    return "paper-only; public data plus local paper ledger; no live Kalshi orders"


def _signed_money(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):.2f}"


def _signed_pct(value: float) -> str:
    return f"{value * 100:+.1f}%"


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def dump_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str)
