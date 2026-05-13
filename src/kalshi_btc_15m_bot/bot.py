from __future__ import annotations

import json
from typing import Any

from .config import BotConfig
from .kalshi_client import KalshiPublicClient
from .ledger import PaperLedger, evaluate_paper_exit, mark_open_trade_to_market, row_to_dict
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

    def scan_once(self) -> dict[str, Any]:
        settled = self.resolve_open_trades()
        managed_positions = self.manage_open_positions()
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
        payload["paper_account"] = account.__dict__
        payload["position_manager"] = {
            "settled_trades": settled,
            "checked_open_positions": len(managed_positions),
            "paper_closed_positions": sum(1 for row in managed_positions if row.get("paper_closed")),
        }
        payload["safety"] = {
            "trading_mode": self.config.trading_mode,
            "enable_live_orders": self.config.enable_live_orders,
            "boundary": "public data + local paper ledger only; no live order endpoint exists in v1",
        }
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

    def status(self) -> dict[str, Any]:
        account = self.ledger.account()
        predictions = [row_to_dict(row) for row in self.ledger.latest_predictions(limit=5)]
        trades = [row_to_dict(row) for row in self.ledger.latest_trades(limit=10)]
        open_positions = self._open_position_marks()
        performance = self.ledger.performance_summary(open_positions)
        return {
            "as_of": now_utc().isoformat(),
            "ledger_path": str(self.config.ledger_path),
            "paper_account": account.__dict__,
            "performance": performance,
            "latest_predictions": predictions,
            "latest_trades": trades,
            "open_positions": open_positions,
            "safety": {
                "trading_mode": self.config.trading_mode,
                "enable_live_orders": self.config.enable_live_orders,
                "live_orders_supported": False,
            },
        }

    def report(self) -> dict[str, Any]:
        return self.status()

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


def format_scan(payload: dict[str, Any]) -> str:
    side = payload.get("side") or "NONE"
    lines = [
        "BTC 15m Kalshi paper scan",
        f"market: {payload['market_ticker']} closes {payload.get('market_close_time')}",
        f"target: {payload.get('target_price')} current: {payload.get('current_price'):.2f}",
        f"prob_yes: {payload['probability_yes']:.3f} prob_no: {payload['probability_no']:.3f}",
        f"action: {payload['action']} side: {side} edge: {payload['edge']:.3f} stake: ${payload['stake_dollars']:.2f}",
        f"paper_trade_id: {payload.get('paper_trade_id') or 'none'}"
        + (f" skip={payload.get('paper_trade_skip_reason')}" if payload.get("paper_trade_skip_reason") else ""),
        f"cash: ${payload['paper_account']['cash']:.2f} open_notional: ${payload['paper_account']['open_notional']:.2f} realized_pnl: ${payload['paper_account']['realized_pnl']:.2f}",
        "boundary: paper-only; no live Kalshi orders",
        "top reasons:",
    ]
    for reason in payload.get("reasons", [])[:6]:
        lines.append(f"- {reason}")
    return "\n".join(lines)


def format_status(payload: dict[str, Any]) -> str:
    account = payload["paper_account"]
    lines = [
        "BTC 15m Kalshi paper bot status",
        f"ledger: {payload['ledger_path']}",
        f"cash: ${account['cash']:.2f} open_notional: ${account['open_notional']:.2f} realized_pnl: ${account['realized_pnl']:.2f}",
        f"open_trades: {account['open_trades']} settled_trades: {account['settled_trades']}",
        "boundary: paper-only; live_orders_supported=false",
    ]
    if payload.get("performance"):
        perf = payload["performance"]
        lines.append(
            f"equity: ${perf['total_equity']:.2f} open_unrealized: {_signed_money(perf['open_unrealized_pnl'])} "
            f"win_rate: {_pct(perf['win_rate'])} expectancy: {_signed_money(perf['expectancy_dollars'])}/trade"
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
    if payload["latest_trades"]:
        lines.append("latest paper trades:")
        for row in payload["latest_trades"][:5]:
            pnl = row["realized_pnl"] if row["realized_pnl"] is not None else 0.0
            lines.append(
                f"- {row['created_at']} {row['market_ticker']} {row['side']} "
                f"{row['status']} notional=${row['notional']:.2f} pnl=${pnl:.2f}"
            )
    return "\n".join(lines)


def format_report(payload: dict[str, Any]) -> str:
    perf = payload["performance"]
    lines = [
        "BTC 15m Kalshi operator report",
        f"ledger: {payload['ledger_path']}",
        f"total_equity: ${perf['total_equity']:.2f}",
        f"realized_pnl: {_signed_money(perf['realized_pnl'])} open_unrealized: {_signed_money(perf['open_unrealized_pnl'])}",
        (
            f"trades: {perf['total_trades']} total / {perf['closed_trades']} closed / "
            f"{perf['open_trades']} open; win_rate: {_pct(perf['win_rate'])}"
        ),
        f"expectancy: {_signed_money(perf['expectancy_dollars'])}/trade roi_on_risk: {_signed_pct(perf['realized_roi_on_risk'])}",
        f"largest_win: {_signed_money(perf['largest_win'])} largest_loss: {_signed_money(perf['largest_loss'])}",
        "boundary: paper-only; live_orders_supported=false",
    ]
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


def _signed_money(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):.2f}"


def _signed_pct(value: float) -> str:
    return f"{value * 100:+.1f}%"


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def dump_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str)
