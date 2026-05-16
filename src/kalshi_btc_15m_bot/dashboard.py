from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .bot import KalshiBTC15MBot
from .paper_performance import collect_paper_trading_performance

DEFAULT_DASHBOARD_PORT = 8792
DEFAULT_REFRESH_SECONDS = 10
DEFAULT_SERVICE_NAMES = (
    "kalshi-btc15m-dashboard.service",
    "kalshi-btc15m-live-prod.service",
    "kalshi-btc15m-live-demo.service",
    "kalshi-btc15m-paper.service",
    "kalshi-btc15m-1s-paper.service",
)
STREAM_WARNING_HISTORY_LIMIT = 8
STREAM_CHART_HISTORY_LIMIT = 900
STREAM_HEALTH_MAX_STALENESS_SECONDS = 15.0


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
        self._history: list[dict[str, Any]] = []
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
            self._history.append(_stream_history_point(payload, now))
            self._history = self._history[-STREAM_CHART_HISTORY_LIMIT:]
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
            history = [_json_safe_dict(point) for point in self._history]
            updated_at = self._updated_at
            staleness_seconds = (now - updated_at).total_seconds() if updated_at is not None else None
            return {
                "started_at": self._started_at.isoformat(),
                "running": self._running,
                "events_seen": self._events_seen,
                "updated_at": updated_at.isoformat() if updated_at is not None else None,
                "staleness_seconds": staleness_seconds,
                "latest": latest,
                "history": history,
                "history_limit": STREAM_CHART_HISTORY_LIMIT,
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
    paper_results_path = _dashboard_paper_results_path(config)
    paper_performance = collect_paper_trading_performance(
        paper_results_path,
        snapshot_path=_dashboard_snapshot_path(config),
    )

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
            "paper_results_1s_path": str(paper_results_path),
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
        "paper_performance": paper_performance,
        "services": _service_statuses(service_names) if include_service_status else {},
        "raw_status": status,
    }


def render_dashboard_html(data: Mapping[str, Any], *, api_path: str = "/api/dashboard") -> str:
    strategy = _mapping(data.get("strategy"))
    portfolio = _mapping(data.get("portfolio"))
    services = _mapping(data.get("services"))
    paper_perf = _mapping(data.get("paper_performance"))
    paper_metrics = _mapping(paper_perf.get("metrics"))
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
    .table-wrap {{ overflow-x:auto; border:1px solid var(--line); border-radius:14px; background:#08101e; }}
    .review-table {{ width:100%; border-collapse:collapse; min-width:1080px; font-size:12px; }}
    .review-table th,.review-table td {{ border-bottom:1px solid var(--line); padding:9px 10px; text-align:left; vertical-align:top; }}
    .review-table th {{ color:var(--muted); text-transform:uppercase; letter-spacing:.08em; font-weight:800; }}
    .review-table tr:last-child td {{ border-bottom:0; }}
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
  {_paper_performance_panel(paper_perf, paper_metrics)}
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


def dashboard_health_response(
    *,
    stream_enabled: bool,
    stream_snapshot: Mapping[str, Any] | None,
    max_staleness_seconds: float = STREAM_HEALTH_MAX_STALENESS_SECONDS,
) -> tuple[dict[str, Any], HTTPStatus]:
    """Build the dashboard healthz JSON body and HTTP status."""
    reasons: list[str] = []
    snapshot = _mapping(stream_snapshot)
    stream_summary: dict[str, Any] = {
        "enabled": stream_enabled,
        "running": bool(snapshot.get("running")) if stream_enabled else False,
        "events_seen": _int_or_none(snapshot.get("events_seen")),
        "updated_at": snapshot.get("updated_at"),
        "staleness_seconds": _float_or_none(snapshot.get("staleness_seconds")),
        "max_staleness_seconds": max_staleness_seconds,
        "last_error": snapshot.get("last_error"),
    }
    if stream_enabled:
        if not snapshot.get("running"):
            reasons.append("stream_not_running")
        if not snapshot.get("latest"):
            reasons.append("stream_latest_missing")
        staleness_seconds = _float_or_none(snapshot.get("staleness_seconds"))
        if staleness_seconds is not None and staleness_seconds > max_staleness_seconds:
            reasons.append("stream_latest_stale")
        if snapshot.get("last_error"):
            reasons.append("stream_last_error")
    payload = {
        "ok": not reasons,
        "generated_at": datetime.now(UTC).isoformat(),
        "reasons": reasons,
        "stream": stream_summary,
    }
    return payload, HTTPStatus.OK if not reasons else HTTPStatus.SERVICE_UNAVAILABLE

def render_stream_dashboard_html(data: Mapping[str, Any], *, api_path: str = "/api/stream") -> str:
    strategy = _mapping(data.get("strategy"))
    stream = _mapping(data.get("stream"))
    latest = _mapping(stream.get("latest"))
    execution = _execution_from_payload(latest)
    execution_blocked_by = execution.get("blocked_by") if isinstance(execution.get("blocked_by"), Sequence) and not isinstance(execution.get("blocked_by"), str) else []
    execution_blocked_by_text = ", ".join(str(item) for item in execution_blocked_by) if execution_blocked_by else "—"
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
    .execution-card {{ border-color:rgba(103,183,255,.45); background:linear-gradient(145deg, rgba(16,24,38,.98), rgba(10,18,30,.98)); }}
    .execution-action {{ font-size:clamp(44px, 8vw, 94px); font-weight:950; letter-spacing:-.08em; line-height:.9; margin:8px 0 14px; }}
    .execution-action.buy_yes {{ color:var(--green); }} .execution-action.buy_no {{ color:var(--red); }} .execution-action.no_trade {{ color:var(--yellow); }}
    .execution-grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin-top:12px; }}
    .execution-item {{ background:rgba(21,31,48,.65); border:1px solid var(--line); border-radius:16px; padding:12px; }}
    details.debug {{ margin-top:14px; }} details.debug summary {{ cursor:pointer; color:var(--blue); font-weight:800; margin-bottom:10px; }}
    .label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.09em; }}
    .value {{ font-size:26px; font-weight:800; letter-spacing:-.04em; margin-top:7px; }}
    .hint {{ color:var(--muted); font-size:13px; margin-top:7px; }}
    .row {{ display:flex; justify-content:space-between; gap:12px; border-top:1px solid var(--line); padding:10px 0; align-items:flex-start; }} .row:first-child {{ border-top:0; padding-top:0; }}
    .quotegrid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:10px; }}
    .quote {{ background:rgba(21,31,48,.65); border:1px solid var(--line); border-radius:16px; padding:14px; }}
    .quote.yes {{ border-color:rgba(53,212,154,.3); }} .quote.no {{ border-color:rgba(255,95,121,.3); }}
    .chart-head {{ display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; }}
    .chart-controls {{ display:flex; align-items:center; gap:8px; color:var(--muted); font-size:13px; }}
    select {{ color:var(--text); background:#0b1220; border:1px solid var(--line); border-radius:12px; padding:8px 10px; }}
    .chart-wrap {{ position:relative; min-height:260px; }}
    canvas {{ width:100%; height:260px; display:block; border-radius:16px; background:linear-gradient(180deg, rgba(21,31,48,.55), rgba(8,12,20,.35)); border:1px solid rgba(36,50,74,.78); }}
    .chart-legend {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:10px; color:var(--muted); font-size:12px; }}
    .legend-dot {{ width:9px; height:9px; display:inline-block; border-radius:99px; margin-right:5px; }}
    .green {{ color:var(--green); }} .red {{ color:var(--red); }} .yellow {{ color:var(--yellow); }} .blue {{ color:var(--blue); }} .purple {{ color:var(--purple); }}
    pre {{ white-space:pre-wrap; overflow-wrap:anywhere; color:var(--muted); background:#080f1b; border:1px solid var(--line); padding:12px; border-radius:14px; max-height:260px; overflow:auto; }}
    footer {{ color:var(--muted); margin-top:18px; font-size:12px; }}
    @media (max-width:950px) {{ .hero,.kpis,.two,.execution-grid {{ grid-template-columns:1fr; }} main {{ padding:14px; }} }}
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

  <section class="panel execution-card">
    <h2>Execution Decision v1</h2>
    <div class="label">ACTION</div>
    <div id="execution-action" class="execution-action {_execution_action_class(execution.get('action'))}">{esc(execution.get('action') or 'NO_TRADE')}</div>
    <div class="execution-grid">
      <div class="execution-item"><div class="label">Size</div><div id="execution-size" class="value">{_money_or_dash(execution.get('size_dollars'))}</div></div>
      <div class="execution-item"><div class="label">Entry</div><div id="execution-entry" class="value">{_num(execution.get('entry_price'), 3)}</div></div>
      <div class="execution-item"><div class="label">Stop</div><div id="execution-stop" class="value">{_num(execution.get('stop_price'), 3)}</div><div class="hint">type <span id="execution-stop-type">{esc(execution.get('stop_type') or 'none')}</span></div></div>
      <div class="execution-item"><div class="label">Take profit</div><div id="execution-take-profit" class="value">{_num(execution.get('take_profit_price'), 3)}</div></div>
      <div class="execution-item"><div class="label">Confidence</div><div id="execution-confidence" class="value">{_pct_or_dash(execution.get('confidence'))}</div></div>
      <div class="execution-item"><div class="label">Regime</div><div id="execution-regime" class="value">{esc(execution.get('regime') or 'unknown')}</div></div>
      <div class="execution-item" style="grid-column:span 2"><div class="label">Blocked by</div><div id="execution-blocked-by" class="value">{esc(execution_blocked_by_text)}</div></div>
    </div>
    <div class="row" style="margin-top:14px"><span class="muted">Reason</span><strong id="execution-reason">{esc(execution.get('reason') or 'waiting for state')}</strong></div>
    <div class="hint" id="stream-health">stream {_stream_status_label(stream)} · last update {_format_seconds(stream.get('staleness_seconds'))} · error {esc(stream.get('last_error') or '—')}</div>
    <div class="hint">{esc(boundary)}</div>
  </section>

  <details class="panel debug">
    <summary>Debug internals</summary>
    <section class="grid hero">
      <div>
        <h2>BTC / contract state</h2>
        <div class="row"><span class="muted">BTC current price</span><strong id="btc-price">{_money_or_dash(latest.get('current_price'))}</strong></div>
        <div class="row"><span class="muted">source</span><strong id="btc-source">{esc(latest.get('btc_source'))}</strong></div>
        <div class="row"><span class="muted">product</span><strong id="btc-product">{esc(latest.get('btc_product'))}</strong></div>
        <div class="row"><span class="muted">as_of</span><strong id="as-of" class="mono">{esc(latest.get('as_of'))}</strong></div>
      </div>
      <div>
        <h2>Market clock</h2>
        <div class="row"><span class="muted">ticker</span><strong id="market-ticker" class="mono">{esc(latest.get('market_ticker'))}</strong></div>
        <div class="row"><span class="muted">target</span><strong id="target-price">{_money_or_dash(latest.get('target_price'))}</strong></div>
        <div class="row"><span class="muted">time till close</span><strong id="seconds-to-close">{_format_seconds(latest.get('seconds_to_close'))}</strong></div>
        <div class="row"><span class="muted">direction</span><strong id="direction-state">{esc(latest.get('direction_state'))}</strong></div>
        <div class="row"><span class="muted">rollover</span><strong id="rollover-state">{_rollover_text(latest)}</strong></div>
      </div>
    </section>

    <section class="grid kpis" style="margin-top:14px">
      {_stream_kpi('old probability model', _pct_or_dash(latest.get('probability_yes')), f"model {_pct_or_dash(latest.get('model_probability_yes'))} · market {_pct_or_dash(latest.get('market_implied_yes'))}", 'old-probability-model')}
      {_stream_kpi('best_ev', _pct_or_dash(latest.get('best_ev_per_dollar')), f"side {latest.get('best_ev_side') or 'NONE'} · $25 EV {_signed_money_or_dash(latest.get('best_ev_reference_profit_dollars'))}", 'best-ev')}
      {_stream_kpi('prob_edge', _pct_or_dash(latest.get('best_edge')), f"spread {_pct_or_dash(latest.get('best_spread'))}", 'prob-edge')}
      {_stream_kpi('legacy monitor', latest.get('monitor_action') or 'NO_EDGE', f"legacy decision {latest.get('decision') or '—'}", 'monitor')}
    </section>

    <section style="margin-top:14px">
      <div class="chart-head">
        <div>
          <h2>Live graph</h2>
          <div id="chart-summary" class="hint">{esc(len(stream.get('history') or []))} retained stream points · newest at {esc(latest.get('as_of'))}</div>
        </div>
        <label class="chart-controls" for="chart-metric">metric
          <select id="chart-metric" aria-label="Chart metric">
            <option value="current_price">BTC price</option>
            <option value="probability_yes">YES probability</option>
            <option value="best_ev_per_dollar">best EV / $</option>
            <option value="best_edge">prob edge</option>
          </select>
        </label>
      </div>
      <div class="chart-wrap"><canvas id="stream-chart" width="1100" height="260" aria-label="Live stream history chart"></canvas></div>
      <div class="chart-legend">
        <span><span class="legend-dot" style="background:var(--blue)"></span><span id="chart-primary-label">selected metric</span></span>
        <span><span class="legend-dot" style="background:rgba(53,212,154,.7)"></span>target line when charting BTC price</span>
      </div>
    </section>

    <section class="grid two">
      <div>
        <h2>YES / NO orderbook</h2>
        <div class="quotegrid">
          <div class="quote yes"><div class="label">YES</div><div class="value">bid <span id="yes-bid">{_num(latest.get('yes_bid'), 3)}</span> / ask <span id="yes-ask">{_num(latest.get('yes_ask'), 3)}</span></div><div class="hint">p_yes <span id="p-yes">{_num(latest.get('probability_yes'), 3)}</span></div></div>
          <div class="quote no"><div class="label">NO</div><div class="value">bid <span id="no-bid">{_num(latest.get('no_bid'), 3)}</span> / ask <span id="no-ask">{_num(latest.get('no_ask'), 3)}</span></div><div class="hint">p_no <span id="p-no">{_num(latest.get('probability_no'), 3)}</span></div></div>
        </div>
        <div class="hint">liquidity <span id="liquidity">{_money_or_dash(latest.get('orderbook_liquidity'))}</span> · valid <span id="orderbook-valid">{esc(latest.get('orderbook_valid'))}</span></div>
      </div>
      <div>
        <h2>Warnings / collector</h2>
        <div class="row"><span class="muted">events seen</span><strong id="events-seen">{esc(stream.get('events_seen'))}</strong></div>
        <div class="row"><span class="muted">last update age</span><strong id="staleness">{_format_seconds(stream.get('staleness_seconds'))}</strong></div>
        <div class="row"><span class="muted">last error</span><strong id="last-error">{esc(stream.get('last_error'))}</strong></div>
        <pre id="warnings-json">{esc(json.dumps(stream.get('warnings') or [], indent=2, default=str))}</pre>
      </div>
    </section>

    <section style="margin-top:14px">
      <h2>Prediction reasons</h2>
      <pre id="prediction-reasons-json">{esc(json.dumps(latest.get('reasons') or [], indent=2, default=str))}</pre>
      <h2>Raw latest payload</h2>
      <pre id="raw-json">{esc(json.dumps(latest or {}, indent=2, sort_keys=True, default=str))}</pre>
    </section>
  </details>
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
const CHART_CONFIG = {{
  current_price: {{label:'BTC price', color:'#67b7ff', fmt:money}},
  probability_yes: {{label:'YES probability', color:'#35d49a', fmt:pct}},
  best_ev_per_dollar: {{label:'best EV / $', color:'#a78bfa', fmt:pct}},
  best_edge: {{label:'prob edge', color:'#ffca58', fmt:pct}}
}};
function chartMetric() {{ const el = document.getElementById('chart-metric'); return el ? el.value : 'current_price'; }}
function finiteNumber(v) {{ const n = Number(v); return Number.isFinite(n) ? n : null; }}
function chartBounds(values, padRatio=0.08) {{ let min = Math.min(...values), max = Math.max(...values); if (min === max) {{ const pad = Math.max(Math.abs(min) * padRatio, 0.01); min -= pad; max += pad; }} else {{ const pad = (max - min) * padRatio; min -= pad; max += pad; }} return [min, max]; }}
function drawSeries(ctx, points, metric, color, xFor, yFor) {{
  ctx.beginPath(); ctx.lineWidth = 2.5; ctx.strokeStyle = color; let started = false;
  points.forEach((point, idx) => {{ const value = finiteNumber(point[metric]); if (value === null) return; const x = xFor(idx); const y = yFor(value); if (!started) {{ ctx.moveTo(x, y); started = true; }} else {{ ctx.lineTo(x, y); }} }});
  if (started) ctx.stroke();
}}
function renderChart(history) {{
  const canvas = document.getElementById('stream-chart'); if (!canvas) return;
  const metric = chartMetric(); const cfg = CHART_CONFIG[metric] || CHART_CONFIG.current_price;
  const rect = canvas.getBoundingClientRect(); const ratio = window.devicePixelRatio || 1; const width = Math.max(320, Math.floor(rect.width || canvas.width)); const height = 260;
  if (canvas.width !== Math.floor(width * ratio) || canvas.height !== Math.floor(height * ratio)) {{ canvas.width = Math.floor(width * ratio); canvas.height = Math.floor(height * ratio); }}
  const ctx = canvas.getContext('2d'); if (!ctx) return; ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, width, height);
  const points = (history || []).filter(point => finiteNumber(point[metric]) !== null).slice(-300);
  ctx.fillStyle = 'rgba(8,12,20,.95)'; ctx.fillRect(0, 0, width, height);
  const pad = {{left:54, right:18, top:18, bottom:34}}; const plotW = Math.max(1, width - pad.left - pad.right); const plotH = Math.max(1, height - pad.top - pad.bottom);
  ctx.strokeStyle = 'rgba(139,155,179,.16)'; ctx.lineWidth = 1; ctx.font = '11px ui-monospace, monospace'; ctx.fillStyle = 'rgba(139,155,179,.9)';
  for (let i=0; i<=4; i++) {{ const y = pad.top + (plotH * i / 4); ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke(); }}
  if (!points.length) {{ ctx.fillText('waiting for stream history...', pad.left, pad.top + 22); setText('chart-summary', '0 retained stream points'); return; }}
  let values = points.map(point => finiteNumber(point[metric])).filter(value => value !== null);
  if (metric === 'current_price') values = values.concat(points.map(point => finiteNumber(point.target_price)).filter(value => value !== null));
  const [min, max] = chartBounds(values); const xFor = idx => pad.left + (points.length <= 1 ? plotW : plotW * idx / (points.length - 1)); const yFor = value => pad.top + plotH - ((value - min) / (max - min)) * plotH;
  for (let i=0; i<=4; i++) {{ const value = max - ((max - min) * i / 4); ctx.fillText(cfg.fmt(value), 8, pad.top + (plotH * i / 4) + 4); }}
  if (metric === 'current_price') drawSeries(ctx, points.filter(point => finiteNumber(point.target_price) !== null), 'target_price', 'rgba(53,212,154,.7)', xFor, yFor);
  drawSeries(ctx, points, metric, cfg.color, xFor, yFor);
  const latest = points[points.length - 1]; setText('chart-primary-label', cfg.label + ' ' + cfg.fmt(latest[metric])); setText('chart-summary', points.length + ' retained stream points · newest at ' + (latest.as_of || latest.recorded_at || '—'));
}}
function actionClass(action) {{ return String(action || 'NO_TRADE').toLowerCase(); }}
function render(data) {{
  const stream = data.stream || {{}}; const p = stream.latest || {{}}; const execution = p.execution_decision || {{action:'NO_TRADE', blocked_by:[]}};
  setText('stream-status', 'stream: ' + (stream.running ? 'running' : 'stopped') + (stream.last_error ? ' / error' : ''));
  setText('stream-health', 'stream ' + (stream.running ? 'running' : 'stopped') + ' · last update ' + seconds(stream.staleness_seconds) + ' · error ' + (stream.last_error || '—'));
  const actionEl = document.getElementById('execution-action');
  if (actionEl) {{ actionEl.textContent = execution.action || 'NO_TRADE'; actionEl.className = 'execution-action ' + actionClass(execution.action); }}
  setText('execution-size', money(execution.size_dollars)); setText('execution-entry', num(execution.entry_price)); setText('execution-stop', num(execution.stop_price)); setText('execution-stop-type', execution.stop_type || 'none'); setText('execution-take-profit', num(execution.take_profit_price));
  setText('execution-confidence', pct(execution.confidence)); setText('execution-regime', execution.regime || 'unknown'); setText('execution-reason', execution.reason || 'waiting for state'); setText('execution-blocked-by', (execution.blocked_by || []).length ? execution.blocked_by.join(', ') : '—');
  setText('btc-price', money(p.current_price)); setText('btc-source', p.btc_source || '—'); setText('btc-product', p.btc_product || '—'); setText('as-of', p.as_of || '—');
  setText('market-ticker', p.market_ticker || '—'); setText('target-price', money(p.target_price)); setText('seconds-to-close', seconds(p.seconds_to_close)); setText('direction-state', p.direction_state || '—');
  setText('rollover-state', p.rollover_status || (p.market_rollover_unsafe ? 'unsafe' : 'clear'));
  kpi('old-probability-model', pct(p.probability_yes), 'model ' + pct(p.model_probability_yes) + ' · market ' + pct(p.market_implied_yes)); kpi('monitor', p.monitor_action || 'NO_EDGE', 'legacy decision ' + (p.decision || '—'));
  kpi('best-ev', pct(p.best_ev_per_dollar), 'side ' + (p.best_ev_side || 'NONE') + ' · $25 EV ' + signedMoney(p.best_ev_reference_profit_dollars));
  kpi('prob-edge', pct(p.best_edge), 'spread ' + pct(p.best_spread));
  setText('yes-bid', num(p.yes_bid)); setText('yes-ask', num(p.yes_ask)); setText('no-bid', num(p.no_bid)); setText('no-ask', num(p.no_ask)); setText('p-yes', num(p.probability_yes)); setText('p-no', num(p.probability_no));
  setText('liquidity', money(p.orderbook_liquidity)); setText('orderbook-valid', String(p.orderbook_valid ?? '—')); setText('events-seen', stream.events_seen ?? '0'); setText('staleness', seconds(stream.staleness_seconds)); setText('last-error', stream.last_error || '—');
  setText('warnings-json', JSON.stringify(stream.warnings || [], null, 2)); setText('prediction-reasons-json', JSON.stringify(p.reasons || [], null, 2)); setText('raw-json', JSON.stringify(p, null, 2)); renderChart(stream.history || []);
}}
async function refresh() {{ try {{ const r = await fetch(API_PATH + tokenQuery(), {{cache:'no-store'}}); if (!r.ok) throw new Error('HTTP ' + r.status); render(await r.json()); }} catch (err) {{ setText('last-error', String(err)); }} }}
const chartSelect = document.getElementById('chart-metric'); if (chartSelect) chartSelect.addEventListener('change', () => renderChart((window.latestStreamData && window.latestStreamData.stream && window.latestStreamData.stream.history) || INITIAL_DATA.stream.history || []));
const originalRender = render; render = function(data) {{ window.latestStreamData = data; originalRender(data); }};
render(INITIAL_DATA); setInterval(refresh, POLL_MS); refresh();
</script>
</body>
</html>"""


def _run_stream_collector_once(
    bot: KalshiBTC15MBot,
    stream_store: StreamSnapshotStore,
    *,
    emit_min_interval_seconds: float,
) -> int:
    from .streaming import RealtimeStateStreamer

    streamer = RealtimeStateStreamer(
        bot,
        json_output=True,
        emit=stream_store.record_line,
        emit_min_interval_seconds=emit_min_interval_seconds,
    )
    return asyncio.run(streamer.run())


def _stream_collector_loop(
    bot: KalshiBTC15MBot,
    stream_store: StreamSnapshotStore,
    *,
    emit_min_interval_seconds: float,
    run_once: Callable[..., int] = _run_stream_collector_once,
    sleep: Callable[[float], None] = time.sleep,
    initial_backoff_seconds: float = 1.0,
    max_backoff_seconds: float = 30.0,
    max_attempts: int | None = None,
) -> None:
    attempts = 0
    reconnect_backoff_seconds = initial_backoff_seconds
    while max_attempts is None or attempts < max_attempts:
        attempts += 1
        stream_store.mark_running(True)
        try:
            exit_code = run_once(
                bot,
                stream_store,
                emit_min_interval_seconds=emit_min_interval_seconds,
            )
            stream_store.record_warning(
                {
                    "warning": "stream_collector_stopped",
                    "exit_code": exit_code,
                    "retry_in_seconds": reconnect_backoff_seconds,
                }
            )
        except Exception as exc:  # noqa: BLE001 - dashboard must display and recover stream failures.
            stream_store.record_error(exc)
        finally:
            stream_store.mark_running(False)
        if max_attempts is not None and attempts >= max_attempts:
            break
        sleep(reconnect_backoff_seconds)
        reconnect_backoff_seconds = min(reconnect_backoff_seconds * 2.0, max_backoff_seconds)


def start_stream_collector(
    bot: KalshiBTC15MBot,
    stream_store: StreamSnapshotStore,
    *,
    emit_min_interval_seconds: float = 1.0,
) -> threading.Thread:
    if emit_min_interval_seconds <= 0:
        raise ValueError("stream_emit_min_interval_seconds must be greater than zero")

    thread = threading.Thread(
        target=_stream_collector_loop,
        kwargs={
            "bot": bot,
            "stream_store": stream_store,
            "emit_min_interval_seconds": emit_min_interval_seconds,
        },
        name="kbtc15-stream-dashboard",
        daemon=True,
    )
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
    token = _dashboard_token_from_env(token)
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
            if path == "/healthz":
                data, status = dashboard_health_response(
                    stream_enabled=stream,
                    stream_snapshot=stream_store.snapshot(),
                )
                self._send_json(data, status=status)
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

        def log_message(self, format: str, *args: Any) -> None:
            # Avoid logging query strings because tokenized URLs may use ?token=.
            parsed = urlparse(self.path)
            safe_path = parsed.path or "/"
            message = _sanitize_dashboard_log_message(self.path, format % args)
            print(f"dashboard {self.address_string()} {safe_path} {message}", flush=True)

        def _send_json(self, data: Mapping[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            self._send(
                json.dumps(data, sort_keys=True, indent=2, default=str).encode("utf-8"),
                "application/json; charset=utf-8",
                status=status,
            )

        def _send(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def _send_auth_required(self) -> None:
            body = b"dashboard token required\n"
            try:
                self.send_response(HTTPStatus.UNAUTHORIZED)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("WWW-Authenticate", 'Bearer realm="kbtc15-dashboard"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

    httpd = ThreadingHTTPServer((host, port), DashboardHandler)
    print(
        f"kbtc15 dashboard listening on http://{host}:{port} "
        f"auth={'enabled' if token else 'disabled'} scan_interval={scan_interval_seconds or 'unknown'}s "
        f"stream={'enabled' if stream else 'disabled'} stream_emit={stream_emit_min_interval_seconds}s",
        flush=True,
    )
    httpd.serve_forever()


def _dashboard_token_from_env(
    token: str | None,
    *,
    env: Mapping[str, str] | None = None,
) -> str | None:
    values = os.environ if env is None else env
    return token or values.get("KALSHI_BTC15M_DASHBOARD_TOKEN") or values.get("DASHBOARD_AUTH_TOKEN")


def _validate_dashboard_auth(host: str, token: str | None) -> None:
    if not _is_loopback_bind_host(host) and not token:
        raise ValueError(
            "Dashboard bound to a non-loopback host requires "
            "KALSHI_BTC15M_DASHBOARD_TOKEN or --token."
        )


def _is_loopback_bind_host(host: str) -> bool:
    normalized = (host or "").strip().lower()
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return True
    if normalized in {"", "0.0.0.0", "::"}:
        return False
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _sanitize_dashboard_log_message(path: str, message: str) -> str:
    parsed = urlparse(path)
    safe_path = parsed.path or "/"
    return message.replace(path, safe_path)


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


def _stream_history_point(payload: Mapping[str, Any], recorded_at: datetime) -> dict[str, Any]:
    execution = _execution_from_payload(payload)
    return {
        "recorded_at": recorded_at.isoformat(),
        "as_of": payload.get("as_of") or payload.get("btc_ts") or recorded_at.isoformat(),
        "market_ticker": payload.get("market_ticker"),
        "current_price": _float_or_none(payload.get("current_price")),
        "target_price": _float_or_none(payload.get("target_price")),
        "probability_yes": _float_or_none(payload.get("probability_yes")),
        "probability_no": _float_or_none(payload.get("probability_no")),
        "yes_bid": _float_or_none(payload.get("yes_bid")),
        "yes_ask": _float_or_none(payload.get("yes_ask")),
        "no_bid": _float_or_none(payload.get("no_bid")),
        "no_ask": _float_or_none(payload.get("no_ask")),
        "best_ev_per_dollar": _float_or_none(payload.get("best_ev_per_dollar")),
        "best_edge": _float_or_none(payload.get("best_edge")),
        "best_spread": _float_or_none(payload.get("best_spread")),
        "seconds_to_close": _float_or_none(payload.get("seconds_to_close")),
        "execution_action": execution.get("action"),
        "execution_side": execution.get("side"),
        "execution_confidence": _float_or_none(execution.get("confidence")),
        "execution_regime": execution.get("regime"),
        "decision": payload.get("decision"),
        "monitor_action": payload.get("monitor_action"),
        "feature_source": payload.get("feature_source"),
        "feature_stale": payload.get("feature_stale"),
        "feature_age_seconds": _feature_age_seconds(payload),
    }


def _feature_age_seconds(payload: Mapping[str, Any]) -> float | None:
    features = payload.get("supabase_features")
    if isinstance(features, Mapping):
        return _float_or_none(features.get("feature_age_seconds"))
    return _float_or_none(payload.get("feature_age_seconds"))


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


def _execution_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = payload.get("execution_decision")
    if isinstance(raw, Mapping):
        blocked_by_raw = raw.get("blocked_by")
        blocked_by = (
            [str(item) for item in blocked_by_raw]
            if isinstance(blocked_by_raw, Sequence) and not isinstance(blocked_by_raw, str)
            else []
        )
        return {
            "action": str(raw.get("action") or "NO_TRADE"),
            "side": str(raw.get("side") or "NONE"),
            "size_dollars": _float_or_none(raw.get("size_dollars")) or 0.0,
            "entry_price": _float_or_none(raw.get("entry_price")),
            "stop_type": str(raw.get("stop_type") or "none"),
            "stop_price": _float_or_none(raw.get("stop_price")),
            "take_profit_price": _float_or_none(raw.get("take_profit_price")),
            "confidence": _float_or_none(raw.get("confidence")) or 0.0,
            "regime": str(raw.get("regime") or payload.get("regime") or "unknown"),
            "reason": str(raw.get("reason") or "waiting for execution decision"),
            "blocked_by": blocked_by,
        }
    decision = str(payload.get("decision") or "legacy_state")
    return {
        "action": "NO_TRADE",
        "side": "NONE",
        "size_dollars": 0.0,
        "entry_price": None,
        "stop_type": "none",
        "stop_price": None,
        "take_profit_price": None,
        "confidence": 0.0,
        "regime": str(payload.get("regime") or "unknown"),
        "reason": f"legacy payload without execution_decision ({decision})",
        "blocked_by": ["missing_execution_decision"],
    }


def _execution_action_class(action: Any) -> str:
    normalized = str(action or "NO_TRADE").lower()
    return normalized if normalized in {"buy_yes", "buy_no", "no_trade"} else "no_trade"


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


def _dashboard_snapshot_path(config: Any) -> Path:
    recorder_owned = Path("data/realtime-snapshots-1s.sqlite3").resolve()
    configured = getattr(config, "realtime_snapshots_path", None)
    configured_path = Path(configured) if configured is not None else None
    data_dir = Path(getattr(config, "data_dir", Path("data")))
    data_dir_candidate = data_dir / "realtime-snapshots-1s.sqlite3"

    if configured_path is not None and recorder_owned.exists():
        try:
            if configured_path.resolve() != recorder_owned:
                return recorder_owned
        except OSError:
            return recorder_owned
    if configured_path is not None and configured_path.exists():
        return configured_path
    if data_dir_candidate.exists():
        return data_dir_candidate
    if recorder_owned.exists():
        return recorder_owned
    if configured_path is not None:
        return configured_path
    return data_dir_candidate


def _dashboard_paper_results_path(config: Any) -> Path:
    configured = getattr(config, "paper_results_1s_path", None)
    if configured is not None:
        return Path(configured)
    data_dir = Path(getattr(config, "data_dir", Path("data")))
    one_second_results = data_dir / "paper-results-1s.sqlite3"
    if one_second_results.exists():
        return one_second_results
    legacy = getattr(config, "ledger_path", None)
    if legacy is not None:
        return Path(legacy)
    return one_second_results


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
    settlement_detail = _paper_settlement_detail(row)
    detail = esc(row.get("created_at"))
    if settlement_detail:
        detail += f" · {esc(settlement_detail)}"
    return (
        "<div class='row'>"
        f"<div><strong>{esc(row.get('status'))} {esc(row.get('side'))}</strong> <span class='badge'>{esc(row.get('market_ticker'))}</span>"
        f"<div class='muted tiny'>{detail}</div></div>"
        f"<div class='mono'>notional={_money_or_dash(row.get('notional'))}<br>pnl={_signed_money_or_dash(pnl)}</div>"
        "</div>"
    )


def _paper_performance_panel(perf: Mapping[str, Any], metrics: Mapping[str, Any]) -> str:
    raw_schema_gaps = perf.get("schema_gaps")
    schema_gaps: list[Any] = raw_schema_gaps if isinstance(raw_schema_gaps, list) else []
    gap_html = "".join(f"<span class='pill'>{esc(gap)}</span>" for gap in schema_gaps[:6])
    if len(schema_gaps) > 6:
        gap_html += f"<span class='pill'>+{len(schema_gaps) - 6} more</span>"
    if not gap_html:
        gap_html = "<span class='pill green'>schema ok</span>"
    return f"""
  <section class="panel" style="margin-top:14px">
    <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap">
      <div>
        <h2>Paper Trading Performance</h2>
        <div class="muted tiny">SQLite-backed paper PnL from <span class="mono">{esc(perf.get('ledger_path'))}</span></div>
      </div>
      <div class="pillrow">{gap_html}</div>
    </div>
    <section class="grid kpis" style="margin-top:14px">
      {_kpi('Realized PnL', _signed_money_or_dash(metrics.get('realized_pnl')), f"closed: {esc(metrics.get('closed_positions'))} · win rate {_pct_or_dash(metrics.get('win_rate'))}", metrics.get('realized_pnl'))}
      {_kpi('Unrealized PnL', _signed_money_or_dash(metrics.get('unrealized_pnl')), f"marked: {esc(metrics.get('marked_open_positions'))} · open: {esc(metrics.get('open_positions'))}", metrics.get('unrealized_pnl'))}
      {_kpi('Total PnL', _signed_money_or_dash(metrics.get('total_pnl')), f"positions: {esc(metrics.get('total_simulated_positions'))} · source {esc(metrics.get('unrealized_source'))}", metrics.get('total_pnl'))}
      {_kpi('Avg trade', _signed_money_or_dash(metrics.get('avg_trade_pnl')), f"avg win {_signed_money_or_dash(metrics.get('avg_win'))} · avg loss {_signed_money_or_dash(metrics.get('avg_loss'))}", metrics.get('avg_trade_pnl'))}
    </section>
    <div id="cumulative-pnl-chart" style="margin-top:14px">{_cumulative_pnl_svg(perf.get('cumulative_pnl'))}</div>
    <section class="panel" style="margin-top:14px;box-shadow:none">
      <h2>Paper PnL Review</h2>
      <div class="muted tiny" style="margin-bottom:10px">Hypothesis table: does slope + above/below have edge, or is it noise?</div>
      {_paper_review_table(perf.get('review_trades'))}
    </section>
    <section class="panel" style="margin-top:14px;box-shadow:none">
      <h2>Paper Review Buckets</h2>
      <div class="muted tiny" style="margin-bottom:10px">Bucketed PnL by strategy, side, expiry, distance, slope, and hold time before changing strategy rules.</div>
      {_paper_review_bucket_table(perf.get('review_buckets'))}
    </section>
    <section class="grid two">
      <div class="panel" style="margin-top:14px;box-shadow:none">
        <h2>Recent paper trades</h2>
        {_rows(perf.get('recent_trades'), _paper_performance_trade_row, 'No paper trades in SQLite yet.')}
      </div>
      <div class="panel" style="margin-top:14px;box-shadow:none">
        <h2>Open marked positions</h2>
        {_rows(perf.get('open_positions'), _paper_performance_position_row, 'No open paper positions.')}
      </div>
    </section>
    <section class="grid two">
      <div class="panel" style="margin-top:14px;box-shadow:none">
        <h2>Grouped by signal</h2>
        {_rows(perf.get('by_signal'), _paper_group_row, 'No signal grouping available.')}
      </div>
      <div class="panel" style="margin-top:14px;box-shadow:none">
        <h2>Grouped by market</h2>
        {_rows(perf.get('by_market'), _paper_group_row, 'No market grouping available.')}
      </div>
    </section>
  </section>"""


def _paper_review_table(value: Any) -> str:
    rows = value if isinstance(value, list) else []
    if not rows:
        return "<p class='muted'>No paper review rows yet. Let the 1s paper collector run first.</p>"
    body = "".join(_paper_review_table_row(_mapping(row)) for row in rows[:25])
    headers = (
        "<tr>"
        "<th>strategy</th>"
        "<th>side</th>"
        "<th>entry_time</th>"
        "<th>exit_time</th>"
        "<th>entry_price</th>"
        "<th>exit_price</th>"
        "<th>pnl</th>"
        "<th>hold_seconds</th>"
        "<th>slope_at_entry</th>"
        "<th>distance_from_strike</th>"
        "<th>seconds_to_expiry</th>"
        "<th>settlement</th>"
        "<th>reason</th>"
        "</tr>"
    )
    return f"<div class='table-wrap'><table class='review-table'><thead>{headers}</thead><tbody>{body}</tbody></table></div>"


def _paper_review_bucket_table(value: Any) -> str:
    bucket_map = value if isinstance(value, Mapping) else {}
    rows: list[str] = []
    for category in (
        "strategy",
        "side",
        "seconds_to_expiry",
        "distance_from_strike",
        "slope_at_entry",
        "hold_seconds",
    ):
        raw_rows = bucket_map.get(category)
        if not isinstance(raw_rows, list):
            continue
        rows.extend(_paper_review_bucket_row(category, _mapping(row)) for row in raw_rows[:12])
    if not rows:
        return "<p class='muted'>No bucketed paper review stats yet.</p>"
    headers = (
        "<tr>"
        "<th>group</th>"
        "<th>bucket</th>"
        "<th>trades</th>"
        "<th>win_rate</th>"
        "<th>avg_pnl</th>"
        "<th>total_pnl</th>"
        "<th>avg_hold_seconds</th>"
        "</tr>"
    )
    return f"<div class='table-wrap'><table class='review-table'><thead>{headers}</thead><tbody>{''.join(rows)}</tbody></table></div>"


def _paper_review_bucket_row(category: str, row: Mapping[str, Any]) -> str:
    return (
        "<tr>"
        f"<td>{esc(category)}</td>"
        f"<td>{esc(row.get('bucket'))}</td>"
        f"<td>{esc(row.get('trades'))}</td>"
        f"<td>{_pct_or_dash(row.get('win_rate'))}</td>"
        f"<td>{_signed_money_or_dash(row.get('avg_pnl'))}</td>"
        f"<td>{_signed_money_or_dash(row.get('total_pnl'))}</td>"
        f"<td>{_num(row.get('avg_hold_seconds'), 0)}</td>"
        "</tr>"
    )


def _paper_review_table_row(row: Mapping[str, Any]) -> str:
    return (
        "<tr>"
        f"<td>{esc(row.get('strategy'))}</td>"
        f"<td>{esc(row.get('side'))}</td>"
        f"<td class='mono'>{esc(row.get('entry_time'))}</td>"
        f"<td class='mono'>{esc(row.get('exit_time'))}</td>"
        f"<td>{_num(row.get('entry_price'), 3)}</td>"
        f"<td>{_num(row.get('exit_price'), 3)}</td>"
        f"<td>{_signed_money_or_dash(row.get('pnl'))}</td>"
        f"<td>{_num(row.get('hold_seconds'), 0)}</td>"
        f"<td>{_num(row.get('slope_at_entry'), 3)}</td>"
        f"<td>{_num(row.get('distance_from_strike'), 2)}</td>"
        f"<td>{_num(row.get('seconds_to_expiry'), 0)}</td>"
        f"<td>{esc(_paper_settlement_label(row))}</td>"
        f"<td>{esc(row.get('reason'))}</td>"
        "</tr>"
    )


def _paper_settlement_label(row: Mapping[str, Any]) -> str:
    label = row.get("settlement_source_label") or _paper_source_label(row.get("settlement_source"))
    if label:
        return str(label)
    if str(row.get("status") or "").upper() == "SETTLED":
        return "source unavailable"
    return "—"


def _paper_settlement_detail(row: Mapping[str, Any]) -> str | None:
    label = row.get("settlement_source_label") or _paper_source_label(row.get("settlement_source"))
    if not label:
        return None
    detail = f"settlement: {label}"
    official_result = row.get("official_result")
    if official_result:
        detail += f" result={official_result}"
    official_value = _float_or_none(row.get("official_expiration_value"))
    if official_value is not None:
        detail += f" official_btc={official_value:,.2f}"
    return detail


def _paper_source_label(value: Any) -> str | None:
    if value is None or value == "":
        return None
    source = str(value)
    if source == "kalshi_official":
        return "official (Kalshi)"
    if source == "coinbase_estimate":
        return "estimated (Coinbase/raw)"
    return source


def _paper_performance_trade_row(row: Mapping[str, Any]) -> str:
    settlement_detail = _paper_settlement_detail(row)
    detail = f"{esc(row.get('created_at'))} · {esc(row.get('exit_reason') or 'open')}"
    if settlement_detail:
        detail += f" · {esc(settlement_detail)}"
    return (
        "<div class='row'>"
        f"<div><strong>{esc(row.get('status'))} {esc(row.get('signal') or row.get('side'))}</strong> <span class='badge'>{esc(row.get('market_ticker'))}</span>"
        f"<div class='muted tiny'>{detail}</div></div>"
        f"<div class='mono'>entry={_num(row.get('entry_price'), 3)} exit={_num(row.get('exit_price'), 3)}<br>notional={_money_or_dash(row.get('notional'))} pnl={_signed_money_or_dash(row.get('realized_pnl'))}</div>"
        "</div>"
    )


def _paper_performance_position_row(row: Mapping[str, Any]) -> str:
    detail = (
        f"mark={_num(row.get('mark_price'), 3)} · {_signed_money_or_dash(row.get('unrealized_pnl'))}"
        if row.get("mark_error") is None
        else f"mark_error={esc(row.get('mark_error'))}"
    )
    return (
        "<div class='row'>"
        f"<div><strong>OPEN {esc(row.get('signal') or row.get('side'))}</strong> <span class='badge'>{esc(row.get('market_ticker'))}</span>"
        f"<div class='muted tiny'>{esc(row.get('created_at'))}</div></div>"
        f"<div class='mono'>entry={_num(row.get('entry_price'), 3)} contracts={_num(row.get('contracts'), 2)}<br>{detail}</div>"
        "</div>"
    )


def _paper_group_row(row: Mapping[str, Any]) -> str:
    title = row.get("signal") or row.get("market_ticker") or row.get("side") or "group"
    subtitle = row.get("market_ticker") if row.get("signal") else row.get("side")
    return (
        "<div class='row'>"
        f"<div><strong>{esc(title)}</strong> <span class='badge'>{esc(subtitle)}</span>"
        f"<div class='muted tiny'>trades {esc(row.get('trades'))} · open {esc(row.get('open_positions'))} · win {_pct_or_dash(row.get('win_rate'))}</div></div>"
        f"<div class='mono'>realized={_signed_money_or_dash(row.get('realized_pnl'))}<br>notional={_money_or_dash(row.get('notional'))}</div>"
        "</div>"
    )


def _cumulative_pnl_svg(value: Any) -> str:
    points = value if isinstance(value, list) else []
    if not points:
        return "<p class='muted'>No closed paper trades yet for cumulative PnL.</p>"
    pnls = [_float_or_none(_mapping(point).get("cumulative_pnl")) for point in points]
    pnls = [pnl for pnl in pnls if pnl is not None]
    if not pnls:
        return "<p class='muted'>No realized PnL points available.</p>"
    width = 760
    height = 180
    pad = 18
    lo = min(0.0, min(pnls))
    hi = max(0.0, max(pnls))
    span = hi - lo or 1.0
    coords: list[str] = []
    for idx, pnl in enumerate(pnls):
        x = pad + (width - pad * 2) * (idx / max(1, len(pnls) - 1))
        y = height - pad - ((pnl - lo) / span) * (height - pad * 2)
        coords.append(f"{x:.1f},{y:.1f}")
    zero_y = height - pad - ((0.0 - lo) / span) * (height - pad * 2)
    latest = pnls[-1]
    return (
        "<svg viewBox='0 0 760 180' role='img' aria-label='Cumulative paper PnL chart' "
        "style='width:100%;height:180px;border:1px solid var(--line);border-radius:14px;background:#08101e'>"
        f"<line x1='{pad}' y1='{zero_y:.1f}' x2='{width - pad}' y2='{zero_y:.1f}' stroke='#223149' stroke-width='1'/>"
        f"<polyline fill='none' stroke='#38d996' stroke-width='3' points='{' '.join(coords)}'/>"
        f"<text x='{pad}' y='24' fill='#93a4bd' font-size='12'>latest {_signed_money_or_dash(latest)}</text>"
        f"<text x='{pad}' y='{height - 8}' fill='#93a4bd' font-size='12'>range {_signed_money_or_dash(lo)} to {_signed_money_or_dash(hi)}</text>"
        "</svg>"
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
