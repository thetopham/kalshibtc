from __future__ import annotations

import asyncio
import html
import json
import os
import subprocess
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .bot import KalshiBTC15MBot

DEFAULT_DASHBOARD_PORT = 8792
DEFAULT_REFRESH_SECONDS = 10
DEFAULT_SERVICE_NAMES = (
    "kalshi-btc15m-dashboard.service",
    "kalshi-btc15m-live-prod.service",
    "kalshi-btc15m-live-demo.service",
    "kalshi-btc15m-paper.service",
)
STREAM_WARNING_HISTORY_LIMIT = 8


class StreamSnapshotStore:
    """Thread-safe latest-state cache fed by the realtime websocket streamer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_at = datetime.now(UTC)
        self._running = False
        self._latest: dict[str, Any] | None = None
        self._updated_at: datetime | None = None
        self._events_seen = 0
        self._warnings: list[dict[str, Any]] = []
        self._last_error: str | None = None

    def mark_running(self, running: bool, *, error: str | None = None) -> None:
        with self._lock:
            self._running = running
            if error:
                self._last_error = _dashboard_error_text(error)

    def record_line(self, text: str) -> None:
        """Record one JSON line emitted by RealtimeStateStreamer."""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            self.record_error(f"stream_output_parse_error: {exc}")
            return
        if not isinstance(payload, Mapping):
            self.record_error("stream_output_parse_error: expected JSON object")
            return
        if payload.get("event") == "market_state" or payload.get("market_ticker"):
            self.record_payload(payload)
            return
        self.record_warning(payload)

    def record_payload(self, payload: Mapping[str, Any]) -> None:
        now = datetime.now(UTC)
        with self._lock:
            self._latest = _json_safe_dict(payload)
            self._updated_at = now
            self._events_seen += 1
            self._last_error = None

    def record_warning(self, payload: Mapping[str, Any]) -> None:
        warning = _json_safe_dict(payload)
        warning.setdefault("as_of", datetime.now(UTC).isoformat())
        with self._lock:
            self._warnings.append(warning)
            self._warnings = self._warnings[-STREAM_WARNING_HISTORY_LIMIT:]
            text = warning.get("error") or warning.get("warning")
            if text:
                self._last_error = _dashboard_error_text(text)

    def record_error(self, error: Any) -> None:
        text = _dashboard_error_text(error)
        self.record_warning({"warning": "stream_collector_error", "error": text})

    def snapshot(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        with self._lock:
            latest = _json_safe_dict(self._latest) if self._latest is not None else None
            warnings = [_json_safe_dict(warning) for warning in self._warnings]
            updated_at = self._updated_at
            staleness_seconds = (now - updated_at).total_seconds() if updated_at is not None else None
            return {
                "started_at": self._started_at.isoformat(),
                "running": self._running,
                "events_seen": self._events_seen,
                "updated_at": updated_at.isoformat() if updated_at is not None else None,
                "staleness_seconds": staleness_seconds,
                "latest": latest,
                "last_warning": warnings[-1] if warnings else None,
                "warning_count": len(warnings),
                "warnings": warnings,
                "last_error": self._last_error,
            }


def collect_dashboard_data(
    bot: KalshiBTC15MBot,
    *,
    service_names: Sequence[str] = DEFAULT_SERVICE_NAMES,
    scan_interval_seconds: float | None = None,
    include_service_status: bool = True,
) -> dict[str, Any]:
    """Collect read-only dashboard data.

    This function intentionally calls status/report surfaces only. It never calls
    scan/run, never constructs order plans, and never submits live orders.
    """
    generated_at = datetime.now(UTC).isoformat()
    try:
        status = bot.status()
        status_error = None
    except Exception as exc:  # noqa: BLE001 - dashboard should fail closed and display the error.
        status = {}
        status_error = str(exc)

    config = bot.config
    safety = status.get("safety") or bot._safety_payload()  # noqa: SLF001 - dashboard is same package.
    live = status.get("live") or {}
    live_account = live.get("account") or {}
    performance = status.get("performance") or {}
    paper_account = status.get("paper_account") or {}

    return {
        "generated_at": generated_at,
        "status_error": status_error,
        "boundary": safety.get("boundary", "unknown"),
        "strategy": {
            "name": "Kalshi BTC 15m",
            "series_ticker": config.kalshi.series_ticker,
            "trading_mode": config.trading_mode,
            "enable_live_orders": config.enable_live_orders,
            "live_environment": config.live.environment,
            "market_data_provider": config.market_data.provider,
            "candle_granularity_seconds": config.market_data.granularity_seconds,
            "scan_interval_seconds": scan_interval_seconds,
            "ledger_path": str(config.ledger_path),
            "data_dir": str(config.data_dir),
        },
        "portfolio": {
            "paper_cash": _float_or_none(paper_account.get("cash")),
            "paper_realized_pnl": _float_or_none(paper_account.get("realized_pnl")),
            "paper_open_notional": _float_or_none(paper_account.get("open_notional")),
            "paper_equity": _float_or_none(performance.get("total_equity")),
            "paper_unrealized_pnl": _float_or_none(performance.get("open_unrealized_pnl")),
            "paper_win_rate": _float_or_none(performance.get("win_rate")),
            "paper_expectancy_dollars": _float_or_none(performance.get("expectancy_dollars")),
            "paper_total_trades": _int_or_none(performance.get("total_trades")),
            "paper_closed_trades": _int_or_none(performance.get("closed_trades")),
            "paper_open_trades": _int_or_none(performance.get("open_trades")),
            "live_balance_dollars": _float_or_none(live_account.get("balance_dollars")),
            "live_portfolio_value_dollars": _float_or_none(live_account.get("portfolio_value_dollars")),
            "live_remote_positions": _int_or_none(live_account.get("nonzero_positions")),
            "live_realized_pnl": _float_or_none(live.get("realized_pnl")),
            "live_account_error": live_account.get("error"),
        },
        "open_paper_positions": status.get("open_positions", []),
        "open_live_positions": live.get("open_positions", []),
        "latest_predictions": status.get("latest_predictions", []),
        "latest_paper_trades": status.get("latest_trades", []),
        "latest_live_orders": live.get("latest_orders", status.get("latest_live_orders", [])),
        "latest_live_fills": live.get("latest_fills", status.get("latest_live_fills", [])),
        "services": _service_statuses(service_names) if include_service_status else {},
        "raw_status": status,
    }


def render_dashboard_html(data: Mapping[str, Any], *, api_path: str = "/api/dashboard") -> str:
    strategy = _mapping(data.get("strategy"))
    portfolio = _mapping(data.get("portfolio"))
    services = _mapping(data.get("services"))
    boundary = str(data.get("boundary") or "unknown")
    mode = str(strategy.get("trading_mode") or "unknown")
    live_enabled = bool(strategy.get("enable_live_orders"))
    generated_at = str(data.get("generated_at") or "unknown")
    status_error = data.get("status_error")
    refresh_seconds = DEFAULT_REFRESH_SECONDS

    service_cards = "".join(_service_card(name, _mapping(status)) for name, status in services.items())
    if not service_cards:
        service_cards = "<p class='muted'>No service status available.</p>"

    html_doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="{refresh_seconds}">
  <title>Kalshi BTC 15m Dashboard</title>
  <style>
    :root {{ color-scheme: dark; --bg:#070b12; --panel:#101827; --panel2:#151f32; --text:#e8eefc; --muted:#93a4bd; --line:#223149; --green:#38d996; --red:#ff647c; --yellow:#f9c74f; --blue:#65b7ff; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; background: radial-gradient(circle at 10% 0%, #12203a 0%, var(--bg) 42%); color: var(--text); }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 22px; }}
    header {{ display: flex; gap: 14px; align-items: flex-start; justify-content: space-between; flex-wrap: wrap; margin-bottom: 18px; }}
    h1 {{ margin: 0; font-size: clamp(26px, 4vw, 42px); letter-spacing: -0.04em; }}
    h2 {{ margin: 0 0 12px; font-size: 18px; }}
    a {{ color: var(--blue); }}
    .sub {{ color: var(--muted); margin-top: 6px; }}
    .pillrow {{ display:flex; gap:8px; flex-wrap:wrap; }}
    .pill {{ border: 1px solid var(--line); background: rgba(21,31,50,.72); border-radius: 999px; padding: 7px 10px; color: var(--muted); font-size: 13px; }}
    .pill.live {{ color: {"var(--green)" if live_enabled else "var(--muted)"}; border-color: {"rgba(56,217,150,.45)" if live_enabled else "var(--line)"}; }}
    .grid {{ display: grid; gap: 14px; }}
    .kpis {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .two {{ grid-template-columns: 1.2fr .8fr; margin-top: 14px; }}
    .panel {{ background: linear-gradient(180deg, rgba(21,31,50,.96), rgba(12,18,29,.96)); border: 1px solid var(--line); border-radius: 18px; padding: 16px; box-shadow: 0 20px 60px rgba(0,0,0,.28); }}
    .kpi .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }}
    .kpi .value {{ font-size: 28px; font-weight: 800; margin-top: 8px; letter-spacing: -.03em; }}
    .kpi .hint {{ color: var(--muted); font-size: 13px; margin-top: 6px; }}
    .green {{ color: var(--green); }} .red {{ color: var(--red); }} .yellow {{ color: var(--yellow); }} .muted {{ color: var(--muted); }}
    .row {{ display:flex; justify-content:space-between; gap:12px; border-top:1px solid var(--line); padding:10px 0; align-items:flex-start; }}
    .row:first-child {{ border-top:0; padding-top:0; }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
    .badge {{ border-radius: 8px; padding: 3px 7px; background: var(--panel2); border:1px solid var(--line); color: var(--muted); font-size:12px; }}
    .cards {{ display:grid; gap:10px; }}
    .tiny {{ font-size: 12px; }}
    pre {{ white-space: pre-wrap; overflow-wrap:anywhere; color: var(--muted); background:#08101e; border:1px solid var(--line); padding:12px; border-radius:12px; }}
    footer {{ color: var(--muted); margin-top: 18px; font-size: 12px; }}
    @media (max-width: 900px) {{ .kpis,.two {{ grid-template-columns: 1fr; }} main {{ padding: 14px; }} }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Kalshi BTC 15m Dashboard</h1>
      <div class="sub">Generated {esc(generated_at)} · refreshes in browser · API <a href="{esc(api_path)}">{esc(api_path)}</a></div>
    </div>
    <div class="pillrow">
      <span class="pill live">mode: {esc(mode)}</span>
      <span class="pill">env: {esc(strategy.get('live_environment'))}</span>
      <span class="pill">scan: {_format_seconds(strategy.get('scan_interval_seconds'))}</span>
      <span class="pill">candles: {_format_seconds(strategy.get('candle_granularity_seconds'))}</span>
    </div>
  </header>
  {_alert(status_error) if status_error else ""}
  <section class="grid kpis">
    {_kpi('Live balance', _money_or_dash(portfolio.get('live_balance_dollars')), 'Authenticated Kalshi account cash', portfolio.get('live_balance_dollars'))}
    {_kpi('Live portfolio', _money_or_dash(portfolio.get('live_portfolio_value_dollars')), f"remote positions: {portfolio.get('live_remote_positions') if portfolio.get('live_remote_positions') is not None else '—'}", portfolio.get('live_portfolio_value_dollars'))}
    {_kpi('Live realized PnL', _signed_money_or_dash(portfolio.get('live_realized_pnl')), 'Bot-owned live fills only', portfolio.get('live_realized_pnl'))}
    {_kpi('Paper equity', _money_or_dash(portfolio.get('paper_equity')), f"win rate: {_pct_or_dash(portfolio.get('paper_win_rate'))}", portfolio.get('paper_equity'))}
  </section>
  <section class="grid two">
    <div class="panel">
      <h2>Boundary</h2>
      <p>{esc(boundary)}</p>
      <div class="pillrow">
        <span class="pill">orders: {esc('enabled with caps' if live_enabled else 'disabled')}</span>
        <span class="pill">market data: {esc(strategy.get('market_data_provider'))}</span>
        <span class="pill mono">{esc(strategy.get('series_ticker'))}</span>
      </div>
      {('<p class="red">Live account error: ' + esc(portfolio.get('live_account_error')) + '</p>') if portfolio.get('live_account_error') else ''}
    </div>
    <div class="panel">
      <h2>Services</h2>
      {service_cards}
    </div>
  </section>
  <section class="grid two">
    <div class="panel">
      <h2>Open live positions</h2>
      {_rows(data.get('open_live_positions'), _live_position_row, 'No bot-owned live positions.')}
    </div>
    <div class="panel">
      <h2>Open paper positions</h2>
      {_rows(data.get('open_paper_positions'), _paper_position_row, 'No open paper positions.')}
    </div>
  </section>
  <section class="grid two">
    <div class="panel">
      <h2>Latest predictions</h2>
      {_rows(data.get('latest_predictions'), _prediction_row, 'No predictions yet.')}
    </div>
    <div class="panel">
      <h2>Latest live orders/fills</h2>
      <h3 class="tiny muted">Orders</h3>
      {_rows(data.get('latest_live_orders'), _live_order_row, 'No live orders yet.')}
      <h3 class="tiny muted">Fills</h3>
      {_rows(data.get('latest_live_fills'), _live_fill_row, 'No live fills yet.')}
    </div>
  </section>
  <section class="panel" style="margin-top:14px">
    <h2>Latest paper trades</h2>
    {_rows(data.get('latest_paper_trades'), _paper_trade_row, 'No paper trades yet.')}
  </section>
  <footer>Read-only dashboard. It calls status surfaces only and has no route that can scan, submit, cancel, or exit orders. Ledger: <span class="mono">{esc(strategy.get('ledger_path'))}</span></footer>
</main>
</body>
</html>"""
    return html_doc


def collect_stream_dashboard_data(
    bot: KalshiBTC15MBot,
    stream_store: StreamSnapshotStore,
    *,
    service_names: Sequence[str] = DEFAULT_SERVICE_NAMES,
    scan_interval_seconds: float | None = None,
    stream_emit_min_interval_seconds: float | None = None,
    include_service_status: bool = True,
) -> dict[str, Any]:
    """Collect cheap read-only data for the live websocket stream dashboard."""
    generated_at = datetime.now(UTC).isoformat()
    config = bot.config
    try:
        safety = bot._safety_payload()  # noqa: SLF001 - dashboard is same package.
        boundary = safety.get("boundary", "unknown")
    except Exception as exc:  # noqa: BLE001 - dashboard should still show stream state.
        safety = {"error": _dashboard_error_text(exc)}
        boundary = "unknown"
    return {
        "generated_at": generated_at,
        "boundary": boundary,
        "strategy": {
            "name": "Kalshi BTC 15m Stream",
            "series_ticker": config.kalshi.series_ticker,
            "trading_mode": config.trading_mode,
            "enable_live_orders": config.enable_live_orders,
            "live_environment": config.live.environment,
            "market_data_provider": config.market_data.provider,
            "market_data_product": getattr(config.market_data, "product_id", None)
            or getattr(config.market_data, "symbol", None),
            "candle_granularity_seconds": config.market_data.granularity_seconds,
            "scan_interval_seconds": scan_interval_seconds,
            "stream_emit_min_interval_seconds": stream_emit_min_interval_seconds,
            "ledger_path": str(config.ledger_path),
            "data_dir": str(config.data_dir),
        },
        "safety": safety,
        "stream": stream_store.snapshot(),
        "services": _service_statuses(service_names) if include_service_status else {},
    }


def render_stream_dashboard_html(data: Mapping[str, Any], *, api_path: str = "/api/stream") -> str:
    strategy = _mapping(data.get("strategy"))
    stream = _mapping(data.get("stream"))
    latest = _mapping(stream.get("latest"))
    boundary = str(data.get("boundary") or latest.get("boundary") or "unknown")
    generated_at = str(data.get("generated_at") or "unknown")
    initial_json = _json_for_script(data)
    poll_ms = max(500, int((_float_or_none(strategy.get("stream_emit_min_interval_seconds")) or 1.0) * 1000))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kalshi BTC Stream</title>
  <style>
    :root {{ color-scheme: dark; --bg:#05070b; --panel:#0f1724; --panel2:#151f30; --glass:rgba(15,23,36,.78); --text:#e7eefb; --muted:#8b9bb3; --line:#24324a; --green:#35d49a; --red:#ff5f79; --yellow:#ffca58; --blue:#67b7ff; --purple:#a78bfa; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font-family:Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; background: radial-gradient(circle at 12% 0%, rgba(103,183,255,.22), transparent 35%), radial-gradient(circle at 88% 8%, rgba(53,212,154,.14), transparent 28%), var(--bg); color:var(--text); }}
    main {{ max-width:1280px; margin:0 auto; padding:22px; }}
    header {{ display:flex; justify-content:space-between; gap:16px; align-items:flex-start; flex-wrap:wrap; margin-bottom:18px; }}
    h1 {{ margin:0; font-size:clamp(30px, 5vw, 56px); letter-spacing:-.06em; line-height:.95; }}
    h2 {{ margin:0 0 12px; font-size:16px; letter-spacing:-.02em; }}
    .sub,.muted {{ color:var(--muted); }}
    .mono {{ font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace; }}
    .pillrow {{ display:flex; flex-wrap:wrap; gap:8px; }}
    .pill {{ border:1px solid var(--line); background:rgba(21,31,48,.72); border-radius:999px; padding:7px 10px; color:var(--muted); font-size:13px; }}
    .pill.good {{ color:var(--green); border-color:rgba(53,212,154,.45); }} .pill.warn {{ color:var(--yellow); border-color:rgba(255,202,88,.45); }} .pill.bad {{ color:var(--red); border-color:rgba(255,95,121,.45); }}
    .grid {{ display:grid; gap:14px; }} .hero {{ grid-template-columns:1.2fr .8fr; }} .kpis {{ grid-template-columns:repeat(4,minmax(0,1fr)); }} .two {{ grid-template-columns:1fr 1fr; margin-top:14px; }}
    .panel {{ background:linear-gradient(180deg, rgba(16,24,38,.94), rgba(8,12,20,.94)); border:1px solid var(--line); border-radius:22px; padding:16px; box-shadow:0 24px 70px rgba(0,0,0,.32); }}
    .big {{ font-size:clamp(38px, 7vw, 78px); font-weight:900; letter-spacing:-.07em; line-height:.92; }}
    .label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.09em; }}
    .value {{ font-size:26px; font-weight:800; letter-spacing:-.04em; margin-top:7px; }}
    .hint {{ color:var(--muted); font-size:13px; margin-top:7px; }}
    .row {{ display:flex; justify-content:space-between; gap:12px; border-top:1px solid var(--line); padding:10px 0; align-items:flex-start; }} .row:first-child {{ border-top:0; padding-top:0; }}
    .quotegrid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:10px; }}
    .quote {{ background:rgba(21,31,48,.65); border:1px solid var(--line); border-radius:16px; padding:14px; }}
    .quote.yes {{ border-color:rgba(53,212,154,.3); }} .quote.no {{ border-color:rgba(255,95,121,.3); }}
    .green {{ color:var(--green); }} .red {{ color:var(--red); }} .yellow {{ color:var(--yellow); }} .blue {{ color:var(--blue); }} .purple {{ color:var(--purple); }}
    pre {{ white-space:pre-wrap; overflow-wrap:anywhere; color:var(--muted); background:#080f1b; border:1px solid var(--line); padding:12px; border-radius:14px; max-height:260px; overflow:auto; }}
    footer {{ color:var(--muted); margin-top:18px; font-size:12px; }}
    @media (max-width:950px) {{ .hero,.kpis,.two {{ grid-template-columns:1fr; }} main {{ padding:14px; }} }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Kalshi BTC Stream</h1>
      <div class="sub">Live websocket BTC + Kalshi orderbook state · API <a class="blue" href="{esc(api_path)}">{esc(api_path)}</a></div>
      <div class="sub tiny">Server rendered {esc(generated_at)} · browser polls every {poll_ms}ms</div>
    </div>
    <div class="pillrow">
      <span id="stream-status" class="pill">stream: {_stream_status_label(stream)}</span>
      <span class="pill">mode: {esc(strategy.get('trading_mode'))}</span>
      <span class="pill">env: {esc(strategy.get('live_environment'))}</span>
      <span class="pill">emit: {_format_seconds(strategy.get('stream_emit_min_interval_seconds'))}</span>
    </div>
  </header>

  <section class="grid hero">
    <div class="panel">
      <div class="label">BTC current price</div>
      <div id="btc-price" class="big">{_money_or_dash(latest.get('current_price'))}</div>
      <div class="pillrow" style="margin-top:12px">
        <span class="pill">source: <span id="btc-source">{esc(latest.get('btc_source'))}</span></span>
        <span class="pill">product: <span id="btc-product">{esc(latest.get('btc_product'))}</span></span>
        <span class="pill">as_of: <span id="as-of">{esc(latest.get('as_of'))}</span></span>
      </div>
      <div class="hint">{esc(boundary)}</div>
    </div>
    <div class="panel">
      <h2>Market clock</h2>
      <div class="row"><span class="muted">ticker</span><strong id="market-ticker" class="mono">{esc(latest.get('market_ticker'))}</strong></div>
      <div class="row"><span class="muted">target</span><strong id="target-price">{_money_or_dash(latest.get('target_price'))}</strong></div>
      <div class="row"><span class="muted">time till close</span><strong id="seconds-to-close">{_format_seconds(latest.get('seconds_to_close'))}</strong></div>
      <div class="row"><span class="muted">direction</span><strong id="direction-state">{esc(latest.get('direction_state'))}</strong></div>
      <div class="row"><span class="muted">rollover</span><strong id="rollover-state">{_rollover_text(latest)}</strong></div>
    </div>
  </section>

  <section class="grid kpis" style="margin-top:14px">
    {_stream_kpi('Decision', latest.get('decision') or '—', 'Only WATCH_ONLY_EV_SIGNAL is actionable', 'decision')}
    {_stream_kpi('Monitor', latest.get('monitor_action') or 'NO_EDGE', 'Signal output after all gates', 'monitor')}
    {_stream_kpi('best_ev', _pct_or_dash(latest.get('best_ev_per_dollar')), f"side {latest.get('best_ev_side') or 'NONE'} · $25 EV {_signed_money_or_dash(latest.get('best_ev_reference_profit_dollars'))}", 'best-ev')}
    {_stream_kpi('prob_edge', _pct_or_dash(latest.get('best_edge')), f"spread {_pct_or_dash(latest.get('best_spread'))}", 'prob-edge')}
  </section>

  <section class="grid two">
    <div class="panel">
      <h2>YES / NO orderbook</h2>
      <div class="quotegrid">
        <div class="quote yes"><div class="label">YES</div><div class="value">bid <span id="yes-bid">{_num(latest.get('yes_bid'), 3)}</span> / ask <span id="yes-ask">{_num(latest.get('yes_ask'), 3)}</span></div><div class="hint">p_yes <span id="p-yes">{_num(latest.get('probability_yes'), 3)}</span></div></div>
        <div class="quote no"><div class="label">NO</div><div class="value">bid <span id="no-bid">{_num(latest.get('no_bid'), 3)}</span> / ask <span id="no-ask">{_num(latest.get('no_ask'), 3)}</span></div><div class="hint">p_no <span id="p-no">{_num(latest.get('probability_no'), 3)}</span></div></div>
      </div>
      <div class="hint">liquidity <span id="liquidity">{_money_or_dash(latest.get('orderbook_liquidity'))}</span> · valid <span id="orderbook-valid">{esc(latest.get('orderbook_valid'))}</span></div>
    </div>
    <div class="panel">
      <h2>Warnings / collector</h2>
      <div class="row"><span class="muted">events seen</span><strong id="events-seen">{esc(stream.get('events_seen'))}</strong></div>
      <div class="row"><span class="muted">last update age</span><strong id="staleness">{_format_seconds(stream.get('staleness_seconds'))}</strong></div>
      <div class="row"><span class="muted">last error</span><strong id="last-error">{esc(stream.get('last_error'))}</strong></div>
      <pre id="warnings-json">{esc(json.dumps(stream.get('warnings') or [], indent=2, default=str))}</pre>
    </div>
  </section>

  <section class="panel" style="margin-top:14px">
    <h2>Raw latest payload</h2>
    <pre id="raw-json">{esc(json.dumps(latest or {}, indent=2, sort_keys=True, default=str))}</pre>
  </section>
  <footer>Read-only stream dashboard; no live orders submitted. The collector uses the same stream-state path and never calls scan, submit, cancel, or live order routes. Status dashboard: <a class="blue" href="/status">/status</a>.</footer>
</main>
<script>
const API_PATH = {json.dumps(api_path)};
const POLL_MS = {poll_ms};
const INITIAL_DATA = {initial_json};
function tokenQuery() {{ const params = new URLSearchParams(window.location.search); return params.has('token') ? '?' + params.toString() : ''; }}
function get(obj, path, fallback='—') {{ let cur = obj; for (const part of path.split('.')) {{ if (!cur || !(part in cur)) return fallback; cur = cur[part]; }} return cur ?? fallback; }}
function money(v) {{ const n = Number(v); return Number.isFinite(n) ? '$' + n.toLocaleString(undefined, {{maximumFractionDigits:2, minimumFractionDigits:2}}) : '—'; }}
function num(v, d=3) {{ const n = Number(v); return Number.isFinite(n) ? n.toFixed(d) : '—'; }}
function pct(v) {{ const n = Number(v); return Number.isFinite(n) ? (n * 100).toFixed(1) + '%' : '—'; }}
function signedMoney(v) {{ const n = Number(v); if (!Number.isFinite(n)) return '—'; return (n >= 0 ? '+$' : '-$') + Math.abs(n).toFixed(2); }}
function seconds(v) {{ const n = Number(v); return Number.isFinite(n) ? Math.round(n) + 's' : '—'; }}
function setText(id, value) {{ const el = document.getElementById(id); if (el) el.textContent = value; }}
function kpi(id, value, hint) {{ setText(id + '-value', value); setText(id + '-hint', hint); }}
function render(data) {{
  const stream = data.stream || {{}}; const p = stream.latest || {{}};
  setText('stream-status', 'stream: ' + (stream.running ? 'running' : 'stopped') + (stream.last_error ? ' / error' : ''));
  setText('btc-price', money(p.current_price)); setText('btc-source', p.btc_source || '—'); setText('btc-product', p.btc_product || '—'); setText('as-of', p.as_of || '—');
  setText('market-ticker', p.market_ticker || '—'); setText('target-price', money(p.target_price)); setText('seconds-to-close', seconds(p.seconds_to_close)); setText('direction-state', p.direction_state || '—');
  setText('rollover-state', p.rollover_status || (p.market_rollover_unsafe ? 'unsafe' : 'clear'));
  kpi('decision', p.decision || '—', 'Only WATCH_ONLY_EV_SIGNAL is actionable'); kpi('monitor', p.monitor_action || 'NO_EDGE', 'side ' + (p.monitor_side || 'NONE'));
  kpi('best-ev', pct(p.best_ev_per_dollar), 'side ' + (p.best_ev_side || 'NONE') + ' · $25 EV ' + signedMoney(p.best_ev_reference_profit_dollars));
  kpi('prob-edge', pct(p.best_edge), 'spread ' + pct(p.best_spread));
  setText('yes-bid', num(p.yes_bid)); setText('yes-ask', num(p.yes_ask)); setText('no-bid', num(p.no_bid)); setText('no-ask', num(p.no_ask)); setText('p-yes', num(p.probability_yes)); setText('p-no', num(p.probability_no));
  setText('liquidity', money(p.orderbook_liquidity)); setText('orderbook-valid', String(p.orderbook_valid ?? '—')); setText('events-seen', stream.events_seen ?? '0'); setText('staleness', seconds(stream.staleness_seconds)); setText('last-error', stream.last_error || '—');
  setText('warnings-json', JSON.stringify(stream.warnings || [], null, 2)); setText('raw-json', JSON.stringify(p, null, 2));
}}
async function refresh() {{ try {{ const r = await fetch(API_PATH + tokenQuery(), {{cache:'no-store'}}); if (!r.ok) throw new Error('HTTP ' + r.status); render(await r.json()); }} catch (err) {{ setText('last-error', String(err)); }} }}
render(INITIAL_DATA); setInterval(refresh, POLL_MS); refresh();
</script>
</body>
</html>"""


def start_stream_collector(
    bot: KalshiBTC15MBot,
    stream_store: StreamSnapshotStore,
    *,
    emit_min_interval_seconds: float = 1.0,
) -> threading.Thread:
    if emit_min_interval_seconds <= 0:
        raise ValueError("stream_emit_min_interval_seconds must be greater than zero")

    def worker() -> None:
        stream_store.mark_running(True)
        try:
            from .streaming import RealtimeStateStreamer

            streamer = RealtimeStateStreamer(
                bot,
                json_output=True,
                emit=stream_store.record_line,
                emit_min_interval_seconds=emit_min_interval_seconds,
            )
            asyncio.run(streamer.run())
        except Exception as exc:  # noqa: BLE001 - dashboard must display stream failures.
            stream_store.record_error(exc)
        finally:
            stream_store.mark_running(False)

    thread = threading.Thread(target=worker, name="kbtc15-stream-dashboard", daemon=True)
    thread.start()
    return thread


def serve_dashboard(
    bot: KalshiBTC15MBot,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_DASHBOARD_PORT,
    token: str | None = None,
    refresh_seconds: int = DEFAULT_REFRESH_SECONDS,
    scan_interval_seconds: float | None = None,
    stream: bool = False,
    stream_emit_min_interval_seconds: float = 1.0,
) -> None:
    token = token or os.getenv("KALSHI_BTC15M_DASHBOARD_TOKEN") or os.getenv("DASHBOARD_AUTH_TOKEN")
    _validate_dashboard_auth(host, token)
    if port < 1 or port > 65535:
        raise ValueError("dashboard port must be between 1 and 65535")
    if refresh_seconds < 1:
        raise ValueError("refresh_seconds must be at least 1")
    if stream_emit_min_interval_seconds <= 0:
        raise ValueError("stream_emit_min_interval_seconds must be greater than zero")

    stream_store = StreamSnapshotStore()
    if stream:
        start_stream_collector(
            bot,
            stream_store,
            emit_min_interval_seconds=stream_emit_min_interval_seconds,
        )

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "kbtc15-dashboard/0.1"

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path == "/health":
                self._send_json({"ok": True, "generated_at": datetime.now(UTC).isoformat()})
                return
            if not _is_authorized(token, parsed.query, self.headers.get("Authorization")):
                self._send_auth_required()
                return
            if path in {"/api/dashboard", "/dashboard.json"}:
                data = collect_dashboard_data(
                    bot,
                    scan_interval_seconds=scan_interval_seconds,
                    include_service_status=True,
                )
                self._send_json(data)
                return
            if path in {"/api/stream", "/stream.json"}:
                data = collect_stream_dashboard_data(
                    bot,
                    stream_store,
                    scan_interval_seconds=scan_interval_seconds,
                    stream_emit_min_interval_seconds=stream_emit_min_interval_seconds,
                    include_service_status=True,
                )
                self._send_json(data)
                return
            if path in {"/stream", "/stream.html"} or (stream and path in {"/", "/index.html"}):
                data = collect_stream_dashboard_data(
                    bot,
                    stream_store,
                    scan_interval_seconds=scan_interval_seconds,
                    stream_emit_min_interval_seconds=stream_emit_min_interval_seconds,
                    include_service_status=True,
                )
                body = render_stream_dashboard_html(data, api_path="/api/stream")
                self._send(body.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path in {"/", "/index.html", "/status", "/status.html"}:
                data = collect_dashboard_data(
                    bot,
                    scan_interval_seconds=scan_interval_seconds,
                    include_service_status=True,
                )
                body = render_dashboard_html(data, api_path="/api/dashboard").replace(
                    f'content="{DEFAULT_REFRESH_SECONDS}"', f'content="{refresh_seconds}"'
                )
                self._send(body.encode("utf-8"), "text/html; charset=utf-8")
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def log_message(self, fmt: str, *args: Any) -> None:
            # Avoid logging query strings because tokenized URLs may use ?token=.
            parsed = urlparse(self.path)
            safe_path = parsed.path or "/"
            message = fmt % args
            print(f"dashboard {self.address_string()} {safe_path} {message}", flush=True)

        def _send_json(self, data: Mapping[str, Any]) -> None:
            self._send(json.dumps(data, sort_keys=True, indent=2, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def _send(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_auth_required(self) -> None:
            body = b"dashboard token required\n"
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("WWW-Authenticate", 'Bearer realm="kbtc15-dashboard"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer((host, port), DashboardHandler)
    print(
        f"kbtc15 dashboard listening on http://{host}:{port} "
        f"auth={'enabled' if token else 'disabled'} scan_interval={scan_interval_seconds or 'unknown'}s "
        f"stream={'enabled' if stream else 'disabled'} stream_emit={stream_emit_min_interval_seconds}s",
        flush=True,
    )
    httpd.serve_forever()


def _validate_dashboard_auth(host: str, token: str | None) -> None:
    public_hosts = {"0.0.0.0", "::", ""}
    if host in public_hosts and not token:
        raise ValueError(
            "Dashboard bound to a public/LAN host requires KALSHI_BTC15M_DASHBOARD_TOKEN or --token."
        )


def _is_authorized(token: str | None, query: str, authorization: str | None) -> bool:
    if not token:
        return True
    if authorization and authorization.strip() == f"Bearer {token}":
        return True
    query_token = parse_qs(query).get("token", [None])[0]
    return query_token == token


def _service_statuses(service_names: Sequence[str]) -> dict[str, dict[str, Any]]:
    statuses: dict[str, dict[str, Any]] = {}
    for name in service_names:
        statuses[name] = _service_status(name)
    return statuses


def _service_status(name: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                name,
                "--property=LoadState,ActiveState,SubState,UnitFileState,ExecMainPID,ExecMainStartTimestamp,FragmentPath",
                "--no-pager",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception as exc:  # noqa: BLE001 - dashboard should render without systemd.
        return {"error": str(exc)}
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value
    fields["returncode"] = str(result.returncode)
    return fields


def _json_safe_dict(payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(dict(payload), default=str))
    except (TypeError, ValueError):
        return {str(key): str(value) for key, value in payload.items()}


def _dashboard_error_text(value: Any) -> str:
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    if len(text) > 300:
        return text[:297] + "..."
    return text or "unknown"


def _json_for_script(data: Mapping[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, default=str).replace("</", "<\\/")


def _stream_status_label(stream: Mapping[str, Any]) -> str:
    if stream.get("running"):
        return "running"
    if stream.get("latest"):
        return "stopped"
    if stream.get("last_error"):
        return "error"
    return "warming up"


def _rollover_text(latest: Mapping[str, Any]) -> str:
    status = latest.get("rollover_status")
    if status:
        parts = [str(status)]
        stale = latest.get("stale_closed_seconds")
        retry = latest.get("rollover_retry_in_seconds")
        if stale is not None:
            parts.append(f"stale {_format_seconds(stale)}")
        if retry is not None:
            parts.append(f"retry {_format_seconds(retry)}")
        return " · ".join(parts)
    return "unsafe" if latest.get("market_rollover_unsafe") else "clear"


def _stream_kpi(label: str, value: Any, hint: Any, field_id: str) -> str:
    return (
        "<div class='panel kpi'>"
        f"<div class='label'>{esc(label)}</div>"
        f"<div id='{esc(field_id)}-value' class='value'>{esc(value)}</div>"
        f"<div id='{esc(field_id)}-hint' class='hint'>{esc(hint)}</div>"
        "</div>"
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any, renderer: Any, empty: str) -> str:
    rows = value if isinstance(value, list) else []
    if not rows:
        return f"<p class='muted'>{esc(empty)}</p>"
    return "<div class='cards'>" + "".join(renderer(_mapping(row)) for row in rows[:8]) + "</div>"


def _service_card(name: str, status: Mapping[str, Any]) -> str:
    active = status.get("ActiveState") or "unknown"
    cls = "green" if active == "active" else "muted"
    return (
        "<div class='row'>"
        f"<div><strong>{esc(name)}</strong><div class='muted tiny'>{esc(status.get('FragmentPath') or '')}</div></div>"
        f"<div><span class='badge {cls}'>{esc(active)}</span><div class='muted tiny'>{esc(status.get('UnitFileState') or '')}</div></div>"
        "</div>"
    )


def _prediction_row(row: Mapping[str, Any]) -> str:
    return (
        "<div class='row'>"
        f"<div><strong>{esc(row.get('action'))}</strong> <span class='badge'>{esc(row.get('market_ticker'))}</span>"
        f"<div class='muted tiny'>{esc(row.get('created_at'))}</div></div>"
        f"<div class='mono'>p_yes={_num(row.get('probability_yes'), 3)}<br>edge={_num(row.get('edge'), 3)}</div>"
        "</div>"
    )


def _live_position_row(row: Mapping[str, Any]) -> str:
    return (
        "<div class='row'>"
        f"<div><strong>LIVE {esc(row.get('side'))}</strong> <span class='badge'>{esc(row.get('market_ticker'))}</span>"
        f"<div class='muted tiny'>avg entry {_num(row.get('avg_entry_price'), 3)}</div></div>"
        f"<div class='mono'>count={_num(row.get('count'), 2)}<br>realized={_signed_money_or_dash(row.get('realized_pnl'))}</div>"
        "</div>"
    )


def _paper_position_row(row: Mapping[str, Any]) -> str:
    if row.get("quote_error"):
        detail = f"quote_error={esc(row.get('quote_error'))}"
    else:
        detail = f"mark {_num(row.get('mark_price'), 3)} · {_signed_money_or_dash(row.get('unrealized_pnl'))}"
    return (
        "<div class='row'>"
        f"<div><strong>PAPER {esc(row.get('side'))}</strong> <span class='badge'>{esc(row.get('market_ticker'))}</span>"
        f"<div class='muted tiny'>entry {_num(row.get('entry_price'), 3)} · exit {esc(row.get('exit_signal') or 'hold')}</div></div>"
        f"<div class='mono'>{detail}</div>"
        "</div>"
    )


def _live_order_row(row: Mapping[str, Any]) -> str:
    oid = row.get("client_order_id") or row.get("id") or "unknown"
    return (
        "<div class='row'>"
        f"<div><strong>{esc(row.get('action'))} {esc(row.get('side'))}</strong> <span class='badge'>{esc(row.get('status'))}</span>"
        f"<div class='muted tiny mono'>{esc(oid)}</div></div>"
        f"<div class='mono'>{esc(row.get('market_ticker'))}<br>count={esc(row.get('count'))}</div>"
        "</div>"
    )


def _live_fill_row(row: Mapping[str, Any]) -> str:
    return (
        "<div class='row'>"
        f"<div><strong>{esc(row.get('action'))} {esc(row.get('side'))}</strong> <span class='badge'>fill</span>"
        f"<div class='muted tiny mono'>{esc(row.get('fill_id') or 'unknown')}</div></div>"
        f"<div class='mono'>{esc(row.get('market_ticker'))}<br>{_num(row.get('price'), 3)} x {_num(row.get('count'), 2)}</div>"
        "</div>"
    )


def _paper_trade_row(row: Mapping[str, Any]) -> str:
    pnl = row.get("realized_pnl")
    return (
        "<div class='row'>"
        f"<div><strong>{esc(row.get('status'))} {esc(row.get('side'))}</strong> <span class='badge'>{esc(row.get('market_ticker'))}</span>"
        f"<div class='muted tiny'>{esc(row.get('created_at'))}</div></div>"
        f"<div class='mono'>notional={_money_or_dash(row.get('notional'))}<br>pnl={_signed_money_or_dash(pnl)}</div>"
        "</div>"
    )


def _kpi(label: str, value: str, hint: str, raw_value: Any = None) -> str:
    cls = ""
    numeric = _float_or_none(raw_value)
    if numeric is not None:
        cls = " green" if numeric > 0 else " red" if numeric < 0 else ""
    return (
        "<div class='panel kpi'>"
        f"<div class='label'>{esc(label)}</div>"
        f"<div class='value{cls}'>{esc(value)}</div>"
        f"<div class='hint'>{esc(hint)}</div>"
        "</div>"
    )


def _alert(message: Any) -> str:
    return f"<section class='panel' style='border-color:rgba(255,100,124,.45); margin-bottom:14px'><strong class='red'>Status error</strong><pre>{esc(message)}</pre></section>"


def esc(value: Any) -> str:
    if value is None:
        return "—"
    return html.escape(str(value), quote=True)


def _format_seconds(value: Any) -> str:
    numeric = _float_or_none(value)
    if numeric is None:
        return "—"
    if numeric.is_integer():
        return f"{int(numeric)}s"
    return f"{numeric:.1f}s"


def _money_or_dash(value: Any) -> str:
    numeric = _float_or_none(value)
    return "—" if numeric is None else f"${numeric:.2f}"


def _signed_money_or_dash(value: Any) -> str:
    numeric = _float_or_none(value)
    if numeric is None:
        return "—"
    sign = "+" if numeric >= 0 else "-"
    return f"{sign}${abs(numeric):.2f}"


def _pct_or_dash(value: Any) -> str:
    numeric = _float_or_none(value)
    return "—" if numeric is None else f"{numeric * 100:.1f}%"


def _num(value: Any, digits: int) -> str:
    numeric = _float_or_none(value)
    return "—" if numeric is None else f"{numeric:.{digits}f}"


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
