from __future__ import annotations

import json
from typing import Any

from .config import BotConfig
from .kalshi_client import KalshiPublicClient
from .ledger import PaperLedger, row_to_dict
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
        self.resolve_open_trades()
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
        account = self.ledger.account()
        payload = prediction.to_jsonable()
        payload["paper_trade_id"] = trade_id
        payload["paper_account"] = account.__dict__
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

    def status(self) -> dict[str, Any]:
        account = self.ledger.account()
        predictions = [row_to_dict(row) for row in self.ledger.latest_predictions(limit=5)]
        trades = [row_to_dict(row) for row in self.ledger.latest_trades(limit=10)]
        return {
            "as_of": now_utc().isoformat(),
            "ledger_path": str(self.config.ledger_path),
            "paper_account": account.__dict__,
            "latest_predictions": predictions,
            "latest_trades": trades,
            "safety": {
                "trading_mode": self.config.trading_mode,
                "enable_live_orders": self.config.enable_live_orders,
                "live_orders_supported": False,
            },
        }

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
        f"paper_trade_id: {payload.get('paper_trade_id') or 'none'}",
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
    if payload["latest_predictions"]:
        lines.append("latest predictions:")
        for row in payload["latest_predictions"][:3]:
            lines.append(
                f"- {row['created_at']} {row['market_ticker']} {row['action']} "
                f"p_yes={row['probability_yes']:.3f} edge={row['edge']:.3f}"
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


def dump_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=str)
