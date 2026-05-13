from __future__ import annotations

import html
import json
import os
import subprocess
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
    "kalshi-btc15m-live-prod.service",
    "kalshi-btc15m-live-demo.service",
    "kalshi-btc15m-paper.service",
)


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


def serve_dashboard(
    bot: KalshiBTC15MBot,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_DASHBOARD_PORT,
    token: str | None = None,
    refresh_seconds: int = DEFAULT_REFRESH_SECONDS,
    scan_interval_seconds: float | None = None,
) -> None:
    token = token or os.getenv("KALSHI_BTC15M_DASHBOARD_TOKEN") or os.getenv("DASHBOARD_AUTH_TOKEN")
    _validate_dashboard_auth(host, token)
    if port < 1 or port > 65535:
        raise ValueError("dashboard port must be between 1 and 65535")
    if refresh_seconds < 1:
        raise ValueError("refresh_seconds must be at least 1")

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
            if path in {"/", "/index.html"}:
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
        f"auth={'enabled' if token else 'disabled'} scan_interval={scan_interval_seconds or 'unknown'}s",
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
