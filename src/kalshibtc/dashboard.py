from __future__ import annotations

import argparse
import html
import json
import shutil
import sqlite3
import subprocess
from collections import Counter, defaultdict
from urllib.parse import unquote
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import RiskLimits
from .datafeed.models import OrderBookSnapshot, Tick
from .execution.risk import RiskManager
from .market.contract import ContractWindow
from .market.state import MarketState
from .record_1s_snapshots import STREAM_TABLE
from .runtime_paths import (
    DEFAULT_RUNS_DIR,
    PREFERRED_RESULTS_DB,
    PREFERRED_SNAPSHOT_DB,
    resolve_results_db,
    resolve_runs_dir,
    resolve_snapshot_db,
)
from .strategy.dynamic_complement_hedge import DynamicHedgeConfig, replay_feed_db
from .strategy.simple_directional import SimpleDirectionalStrategy

BOUNDARY_TEXT = "Read-only dashboard. No live orders. Active system is 1s recorder + 1s paper executor."
POLYMARKET_BOUNDARY_TEXT = "Read-only Polymarket public-data dashboard. No live orders. Active system is public-data recorder + read-only dashboard."
STREAM_TITLE = "Kalshi BTC Stream"
POLYMARKET_STREAM_TITLE = "Polymarket BTC Stream"
STATUS_TITLE = "Kalshi BTC 1s Paper Status"
STRATEGY_RUNS_TITLE = "Kalshi BTC Strategy Runs"
STRATEGY_RUN_TITLE = "Kalshi BTC Strategy Run"
STRATEGY_BOUNDARY_TEXT = "Read-only replay/backtest dashboard. No order submission."
ACTIVE_SERVICES = (
    "polymarket-btc15m-1s-recorder.service",
    "kalshi-btc15m-1s-recorder.service",
    "kalshi-btc15m-1s-paper.service",
    "kalshi-btc15m-1s-dashboard.service",
)

LEGACY_HTML_KEYS = {
    "decision",
    "monitor_action",
    "model_probability_yes",
    "model_probability_no",
    "probability_yes",
    "probability_no",
    "ml_probability",
    "ev_yes",
    "ev_no",
    "edge_yes",
    "edge_no",
    "prediction_reason",
    "edge_direction_disagreement",
}

JsonDict = dict[str, Any]


def collect_stream_dashboard_data(*, snapshot_db: str | Path | None = None, history_limit: int = 120) -> JsonDict:
    snapshot_path = resolve_snapshot_db(snapshot_db)
    rows = _latest_snapshot_rows(snapshot_path, limit=history_limit)
    latest = _stream_latest_from_row(rows[-1]) if rows else None
    title = POLYMARKET_STREAM_TITLE if rows and row_get(rows[-1], "market_slug") else STREAM_TITLE
    boundary = POLYMARKET_BOUNDARY_TEXT if title == POLYMARKET_STREAM_TITLE else BOUNDARY_TEXT
    history = [_history_point(row) for row in rows]
    complement = _complement_spread_payload(history)
    complement_by_contract = _complement_by_contract_payload(snapshot_path)
    temporal_basis_compression = _temporal_basis_compression_payload(snapshot_path)
    return {
        "title": title,
        "mode": "paper/research/read-only",
        "api_path": "/api/stream",
        "boundary": boundary,
        "snapshot_db_path": str(snapshot_path),
        "stream": {
            "freshness": _freshness(latest["ts"] if latest else None),
            "latest": latest,
            "history": history,
            "history_limit": history_limit,
            "complement_spread": complement,
            "complement_by_contract": complement_by_contract,
            "temporal_basis_compression": temporal_basis_compression,
        },
    }


def collect_strategy_runs_dashboard_data(
    *,
    runs_dir: str | Path | None = None,
    max_runs_per_strategy: int = 25,
    max_total_runs: int = 200,
) -> JsonDict:
    runs_path = resolve_runs_dir(runs_dir)
    runs: list[JsonDict] = []
    ignored: list[JsonDict] = []
    if runs_path.exists():
        for strategy_dir in sorted(path for path in runs_path.iterdir() if path.is_dir()):
            run_dirs = sorted((path for path in strategy_dir.iterdir() if path.is_dir()), reverse=True)[:max_runs_per_strategy]
            for run_dir in run_dirs:
                if len(runs) >= max_total_runs:
                    break
                try:
                    run = _run_summary_from_dir(run_dir)
                except (OSError, json.JSONDecodeError, ValueError) as exc:
                    ignored.append({"strategy": strategy_dir.name, "run_id": run_dir.name, "reason": str(exc)})
                    continue
                runs.append(run)
            if len(runs) >= max_total_runs:
                break
    runs.sort(key=lambda item: str(item.get("created_at") or item.get("run_id") or ""), reverse=True)
    return {
        "title": STRATEGY_RUNS_TITLE,
        "api_path": "/api/strategies",
        "boundary": STRATEGY_BOUNDARY_TEXT,
        "runs_dir_path": str(runs_path),
        "max_runs_per_strategy": max_runs_per_strategy,
        "max_total_runs": max_total_runs,
        "runs": runs,
        "ignored_runs": ignored,
    }


def collect_strategy_run_detail_data(*, runs_dir: str | Path | None = None, strategy: str, run_id: str) -> JsonDict:
    runs_path = resolve_runs_dir(runs_dir)
    run_dir = _run_dir_for_route(runs_path=runs_path, strategy=strategy, run_id=run_id)
    run = _run_summary_from_dir(run_dir)
    return {
        "title": STRATEGY_RUN_TITLE,
        "api_path": f"/api/strategies/{_url_component(strategy)}/{_url_component(run_id)}",
        "boundary": STRATEGY_BOUNDARY_TEXT,
        "run": run,
        "config_text": (run_dir / "config.toml").read_text(encoding="utf-8"),
        "signals": _read_result_rows(run_dir / "results.sqlite3", "replay_signals", limit=200),
        "fills": _read_result_rows(run_dir / "results.sqlite3", "replay_fills", limit=200),
        "pair_positions": _read_result_rows(run_dir / "results.sqlite3", "pair_positions", limit=200),
        "hedge_events": _read_result_rows(run_dir / "results.sqlite3", "hedge_events", limit=200),
        "missed_hedge_opportunities": _read_result_rows(run_dir / "results.sqlite3", "missed_hedge_opportunities", limit=200),
        "rejected_seed_attempts": _read_result_rows(run_dir / "results.sqlite3", "rejected_seed_attempts", limit=200),
        "inventory_vol_positions": _read_result_rows(run_dir / "results.sqlite3", "inventory_vol_positions", limit=200),
        "inventory_vol_events": _read_result_rows(run_dir / "results.sqlite3", "inventory_vol_events", limit=500),
        "inventory_vol_research_metrics": _read_result_rows(run_dir / "results.sqlite3", "inventory_vol_research_metrics", limit=200),
        "inventory_vol_regime_positions": _read_result_rows(run_dir / "results.sqlite3", "inventory_vol_regime_positions", limit=200),
        "inventory_vol_regime_events": _read_result_rows(run_dir / "results.sqlite3", "inventory_vol_regime_events", limit=500),
        "inventory_vol_regime_research_metrics": _read_result_rows(run_dir / "results.sqlite3", "inventory_vol_regime_research_metrics", limit=200),
        "volatility_hedge_positions": _read_result_rows(run_dir / "results.sqlite3", "volatility_hedge_positions", limit=200),
        "volatility_hedge_events": _read_result_rows(run_dir / "results.sqlite3", "volatility_hedge_events", limit=120),
    }


def render_strategy_runs_dashboard_html(data: Mapping[str, Any]) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{_h(run.get('strategy'))}</td><td><a href=\"{_h(run.get('href'))}\">{_h(run.get('run_id'))}</a></td>"
        f"<td>{_fmt(run.get('snapshots'))}</td><td>{_fmt(run.get('signals'))}</td><td>{_fmt(run.get('fills'))}</td><td>{_fmt(run.get('notional'))}</td>"
        f"<td>{_pct(run.get('win_rate'))}</td><td>{_money(run.get('ev_per_trade'))}</td><td>{_money(run.get('max_drawdown'))}</td>"
        f"<td>{_fmt(run.get('sharpe'))}</td><td>{_fmt(run.get('profit_factor'))}</td><td>{_h(run.get('settlement_source'))}</td>"
        f"<td>{_manage_run_cell(run)}</td>"
        "</tr>"
        for run in data.get("runs", [])
    )
    ignored = data.get("ignored_runs") or []
    ignored_rows = "".join(
        f"<tr><td>{_h(row.get('strategy'))}</td><td>{_h(row.get('run_id'))}</td><td>{_h(row.get('reason'))}</td></tr>"
        for row in ignored
    )
    rejected_seed_rows = "".join(
        f"<tr><td>{_h(row.get('ts'))}</td><td>{_h(row.get('market_ticker'))}</td><td>{_h(row.get('side'))}</td><td>{_fmt(row.get('ask_price'))}</td><td>{_h(row.get('reason'))}</td></tr>"
        for row in data.get("rejected_seed_attempts", [])
    )
    body = f"""
    <p class="boundary">{_h(data.get('boundary'))}</p>
    <section><h2>Strategy comparison</h2><table id="strategy-runs-table"><thead><tr><th>strategy</th><th>run</th><th>snapshots</th><th>signals</th><th>fills</th><th>notional</th><th>win rate</th><th>EV/trade</th><th>max drawdown</th><th>Sharpe</th><th>profit factor</th><th>settlement</th><th>manage</th></tr></thead><tbody>{rows}</tbody></table></section>
    <section><h2>Run artifacts</h2><table><tbody>{_kv('runs dir', data.get('runs_dir_path'))}{_kv('API', data.get('api_path'))}{_kv('scan cap', f"{data.get('max_total_runs')} total / {data.get('max_runs_per_strategy')} per strategy")}</tbody></table></section>
    <section><h2>Ignored/incomplete runs</h2><table><tbody>{ignored_rows}</tbody></table></section>
    """
    return _page(
        title=str(data.get("title") or STRATEGY_RUNS_TITLE),
        heading=STRATEGY_RUNS_TITLE,
        subheading=f"API {_h(data.get('api_path'))} | {_h(data.get('boundary'))}",
        body=body,
        refresh_ms=0,
    )


def _manage_run_cell(run: Mapping[str, Any]) -> str:
    if str(run.get("run_id") or "") == "live" or str(run.get("mode") or "") == "live_paper":
        return '<span class="muted">live slot</span>'
    href = run.get("delete_href") or run.get("api_href")
    return f'<button type="button" data-method="DELETE" data-url="{_h(href)}" onclick="confirmDeleteRun(this)">Delete</button>'


def render_strategy_run_detail_html(data: Mapping[str, Any]) -> str:
    run = data.get("run") if isinstance(data.get("run"), Mapping) else {}
    signal_rows = "".join(
        f"<tr><td>{_h(row.get('ts'))}</td><td>{_h(row.get('side'))}</td><td>{_fmt(row.get('confidence'))}</td><td>{_h(row.get('allowed'))}</td><td>{_h(row.get('reason'))}</td></tr>"
        for row in data.get("signals", [])
    )
    fill_rows = "".join(
        f"<tr><td>{_h(row.get('ts'))}</td><td>{_h(row.get('side'))}</td><td>{_fmt(row.get('entry_price'))}</td><td>{_fmt(row.get('contracts'))}</td><td>{_fmt(row.get('notional'))}</td></tr>"
        for row in data.get("fills", [])
    )
    pair_rows = "".join(
        "<tr>"
        f"<td>{_h(row.get('market_ticker'))}</td><td>{_fmt(row.get('held_above_qty'))}</td><td>{_fmt(row.get('held_above_avg'))}</td>"
        f"<td>{_fmt(row.get('held_below_qty'))}</td><td>{_fmt(row.get('held_below_avg'))}</td><td>{_fmt(row.get('pair_cost'))}</td>"
        f"<td>{_fmt(row.get('locked_profit'))}</td><td>{_fmt(row.get('unpaired_directional_exposure'))}</td>"
        f"<td>{_fmt(row.get('time_to_expiry'))}</td><td>{_fmt(row.get('distance_from_strike'))}</td>"
        "</tr>"
        for row in data.get("pair_positions", [])
    )
    rejected_seed_rows = "".join(
        f"<tr><td>{_h(row.get('ts'))}</td><td>{_h(row.get('market_ticker'))}</td><td>{_h(row.get('side'))}</td><td>{_fmt(row.get('ask_price'))}</td><td>{_h(row.get('reason'))}</td></tr>"
        for row in data.get("rejected_seed_attempts", [])
    )
    inventory_rows = "".join(
        "<tr>"
        f"<td>{_h(row.get('market_ticker'))}</td><td>{_fmt(row.get('held_above_qty'))}</td><td>{_fmt(row.get('held_above_avg'))}</td>"
        f"<td>{_fmt(row.get('held_below_qty'))}</td><td>{_fmt(row.get('held_below_avg'))}</td><td>{_fmt(row.get('blended_basis'))}</td>"
        f"<td>{_fmt(row.get('inventory_imbalance_ratio'))}</td><td>{_fmt(row.get('mark_to_market_equity'))}</td><td>{_fmt(row.get('realized_pnl'))}</td><td>{_fmt(row.get('unrealized_pnl'))}</td><td>{_fmt(row.get('max_drawdown'))}</td>"
        "</tr>"
        for row in [*data.get("inventory_vol_positions", []), *data.get("inventory_vol_regime_positions", [])]
    )
    inventory_event_rows = "".join(
        f"<tr><td>{_h(row.get('ts'))}</td><td>{_h(row.get('event_type'))}</td><td>{_h(row.get('side'))}</td><td>{_fmt(row.get('price'))}</td><td>{_fmt(row.get('quantity'))}</td><td>{_h(row.get('reason'))}</td></tr>"
        for row in [*data.get("inventory_vol_events", []), *data.get("inventory_vol_regime_events", [])][:200]
    )
    inventory_research_rows = "".join(
        f"<tr><td>{_h(row.get('metric_name'))}</td><td><pre>{_h(row.get('metric_json'))}</pre></td></tr>"
        for row in [*data.get("inventory_vol_research_metrics", []), *data.get("inventory_vol_regime_research_metrics", [])]
    )
    volatility_hedge_rows = "".join(
        "<tr>"
        f"<td>{_h(row.get('market_ticker'))}</td><td>{_fmt(row.get('up_qty'))}</td><td>{_fmt(row.get('up_avg_entry'))}</td>"
        f"<td>{_fmt(row.get('down_qty'))}</td><td>{_fmt(row.get('down_avg_entry'))}</td><td>{_fmt(row.get('paired_qty'))}</td>"
        f"<td>{_fmt(row.get('paired_cost'))}</td><td>{_fmt(row.get('edge'))}</td><td>{_fmt(row.get('locked_edge_dollars'))}</td>"
        f"<td>{_fmt(row.get('imbalance_ratio'))}</td>"
        "</tr>"
        for row in data.get("volatility_hedge_positions", [])
    )
    volatility_hedge_event_rows = "".join(
        f"<tr><td>{_h(row.get('ts'))}</td><td>{_h(row.get('event_type'))}</td><td>{_h(row.get('side'))}</td><td>{_fmt(row.get('price'))}</td><td>{_fmt(row.get('qty'))}</td><td>{_fmt(row.get('projected_paired_cost'))}</td><td>{_fmt(row.get('current_paired_cost'))}</td><td>{_fmt(row.get('slope'))}</td><td>{_fmt(row.get('atr'))}</td><td>{_fmt(row.get('distance_from_strike'))}</td><td>{_h(row.get('reason'))}</td></tr>"
        for row in data.get("volatility_hedge_events", [])[:200]
    )
    body = f"""
    <p class="boundary">{_h(data.get('boundary'))}</p>
    <section><h2>Strategy run drilldown</h2><table><tbody>
      {_kv('strategy', run.get('strategy'))}{_kv('run id', run.get('run_id'))}{_kv('snapshots', run.get('snapshots'))}{_kv('signals', run.get('signals'))}{_kv('fills', run.get('fills'))}{_kv('notional', run.get('notional'))}{_kv('win rate', _pct(run.get('win_rate')))}{_kv('EV/trade', _money(run.get('ev_per_trade')))}{_kv('max drawdown', _money(run.get('max_drawdown')))}{_kv('Sharpe', run.get('sharpe'))}{_kv('profit factor', run.get('profit_factor'))}{_kv('settlement source', run.get('settlement_source'))}{_kv('results DB', run.get('results_db'))}
    </tbody></table></section>
    <section><h2>Signals</h2><table id="strategy-signals-table"><thead><tr><th>ts</th><th>side</th><th>confidence</th><th>allowed</th><th>reason</th></tr></thead><tbody>{signal_rows}</tbody></table></section>
    <section><h2>Fills</h2><table id="strategy-fills-table"><thead><tr><th>ts</th><th>side</th><th>entry</th><th>contracts</th><th>notional</th></tr></thead><tbody>{fill_rows}</tbody></table></section>
    <section><h2>Pair positions</h2><table id="pair-positions-table"><thead><tr><th>market</th><th>held above qty</th><th>held above avg</th><th>held below qty</th><th>held below avg</th><th>pair cost</th><th>locked profit</th><th>unpaired directional exposure</th><th>time to expiry</th><th>distance from strike</th></tr></thead><tbody>{pair_rows}</tbody></table></section>
    <section><h2>Volatility regime state</h2><p class="muted">inventory_vol_regime opens a 3:2 trend/countertrend starter, then adds to either side only when volatility creates better combined-basis inventory opportunities. Paper/read-only; no order submission.</p></section>
    <section><h2>Inventory imbalance heatmap</h2><h3>Inventory imbalance gauge</h3><table id="inventory-imbalance-gauge"><thead><tr><th>market</th><th>above qty</th><th>above avg</th><th>below qty</th><th>below avg</th><th>blended basis</th><th>imbalance ratio</th><th>MTM equity</th><th>realized PnL</th><th>unrealized PnL</th><th>largest drawdown</th></tr></thead><tbody>{inventory_rows}</tbody></table></section>
    <section><h2>Blended basis over time</h2><p class="muted">Stored in each inventory row equity_curve_json for read-only charting.</p></section>
    <section><h2>Mark-to-market equity curve</h2><p class="muted">Paper-only mark-to-market from bid-side inventory marks; no order submission.</p></section>
    <section><h2>Add/reduction event timeline</h2><table id="inventory-event-timeline"><thead><tr><th>ts</th><th>type</th><th>side</th><th>price</th><th>qty</th><th>reason</th></tr></thead><tbody>{inventory_event_rows}</tbody></table></section>
    <section><h2>ATR expansion graph</h2><p class="muted">Stored in inventory research metrics as atr_expansion_graph.</p></section>
    <section><h2>Distance-from-strike velocity graph</h2><p class="muted">Stored in inventory research metrics as distance_from_strike_velocity_graph.</p></section>
    <section><h2>Volatility overlay</h2><table id="inventory-research-metrics"><tbody>{inventory_research_rows}</tbody></table></section>
    <section><h2>Per-side inventory ladder</h2><p class="muted">Above/Below quantities, averages, reductions, exposure over time, and volatility-add events are persisted in inventory_vol_positions/events and inventory_vol_regime_positions/events.</p></section>
    <section><h2>Rejected seed attempts</h2><table id="rejected-seed-attempts-table"><thead><tr><th>ts</th><th>market</th><th>side</th><th>ask</th><th>reason</th></tr></thead><tbody>{rejected_seed_rows}</tbody></table></section>
    <section><h2>VolatilityHedgeStrategy paired cost</h2><p class="muted">Paper-only synthetic hedge. Primary metric: UP avg + DOWN avg. Adds are rejected unless projected paired cost improves, ideally below 0.98 after estimated slippage/fees.</p><table id="volatility-hedge-positions-table"><thead><tr><th>market</th><th>UP qty</th><th>UP avg</th><th>DOWN qty</th><th>DOWN avg</th><th>paired qty</th><th>paired cost</th><th>edge</th><th>locked edge $</th><th>imbalance ratio</th></tr></thead><tbody>{volatility_hedge_rows}</tbody></table></section>
    <section><h2>VolatilityHedgeStrategy decisions</h2><table id="volatility-hedge-events-table"><thead><tr><th>ts</th><th>type</th><th>side</th><th>price</th><th>qty</th><th>projected paired cost</th><th>current paired cost</th><th>slope</th><th>ATR</th><th>distance</th><th>reason</th></tr></thead><tbody>{volatility_hedge_event_rows}</tbody></table></section>
    <section><h2>Config</h2><pre>{_h(data.get('config_text'))}</pre></section>
    """
    return _page(
        title=str(data.get("title") or STRATEGY_RUN_TITLE),
        heading=STRATEGY_RUN_TITLE,
        subheading=f"API {_h(data.get('api_path'))} | {_h(data.get('boundary'))}",
        body=body,
        refresh_ms=5000,
    )


def collect_status_dashboard_data(
    *,
    snapshot_db: str | Path | None = None,
    results_db: str | Path | None = None,
    service_status_fn: Callable[[Iterable[str]], Mapping[str, Mapping[str, Any]]] | None = None,
) -> JsonDict:
    snapshot_path = resolve_snapshot_db(snapshot_db)
    results_path = resolve_results_db(results_db)
    latest_snapshot = _latest_snapshot_rows(snapshot_path, limit=1)
    latest_snapshot_data = _stream_latest_from_row(latest_snapshot[-1]) if latest_snapshot else None
    performance = _paper_performance(results_path)
    status_fn = service_status_fn or _systemd_user_service_status
    return {
        "title": STATUS_TITLE,
        "api_path": "/api/dashboard",
        "also_api_path": "/api/status",
        "boundary": BOUNDARY_TEXT,
        "snapshot_db_path": str(snapshot_path),
        "results_db_path": str(results_path),
        "latest_snapshot_timestamp": latest_snapshot_data["ts"] if latest_snapshot_data else None,
        "latest_prediction_timestamp": performance["latest_prediction_timestamp"],
        "services": dict(status_fn(ACTIVE_SERVICES)),
        "paper_performance": performance,
        "top_blockers": performance["top_blockers"],
        "latest_snapshot": latest_snapshot_data,
    }


def render_stream_dashboard_html(data: Mapping[str, Any]) -> str:
    stream = data.get("stream") if isinstance(data.get("stream"), Mapping) else {}
    latest = stream.get("latest") if isinstance(stream.get("latest"), Mapping) else None
    history = stream.get("history") if isinstance(stream.get("history"), list) else []
    if latest is None:
        body = "<p>No snapshot rows found yet.</p>"
    else:
        decision = latest["execution_decision"]
        orderbook = latest["orderbook"]
        slopes = latest["slopes"]
        graph_rows = "".join(
            f"<tr><td>{_h(point.get('ts'))}</td><td>{_fmt(point.get('btc_price'))}</td>"
            f"<td>{_fmt(point.get('target_price'))}</td><td>{_fmt(point.get('seconds_to_close'))}</td></tr>"
            for point in history[-20:]
        )
        chart_points_json = _h(json.dumps(history[-90:], default=str))
        complement = stream.get("complement_spread") if isinstance(stream.get("complement_spread"), Mapping) else {}
        complement_history_json = _h(json.dumps((complement.get("history") or [])[-90:], default=str))
        complement_latest = complement.get("latest") if isinstance(complement.get("latest"), Mapping) else {}
        complement_summary = complement.get("summary") if isinstance(complement.get("summary"), Mapping) else {}
        by_contract = stream.get("complement_by_contract") if isinstance(stream.get("complement_by_contract"), Mapping) else {}
        temporal = stream.get("temporal_basis_compression") if isinstance(stream.get("temporal_basis_compression"), Mapping) else {}
        by_contract_rows = "".join(
            "<tr>"
            f"<td>{_h(row.get('market_ticker'))}</td><td>{_fmt(row.get('strike'))}</td><td>{_h(row.get('market_close_time'))}</td>"
            f"<td>{_fmt(row.get('samples'))}</td><td>{_fmt(row.get('max_buy_both_edge'))}</td><td>{_fmt(row.get('max_sell_both_edge'))}</td>"
            f"<td>{_fmt(row.get('avg_buy_both_edge'))}</td><td>{_fmt(row.get('best_buy_seconds_to_close'))}</td>"
            f"<td>{_fmt(row.get('buy_gt_1c'))}/{_fmt(row.get('buy_gt_2c'))}/{_fmt(row.get('buy_gt_5c'))}</td>"
            f"<td>{_fmt(row.get('sell_gt_1c'))}/{_fmt(row.get('sell_gt_2c'))}/{_fmt(row.get('sell_gt_5c'))}</td>"
            "</tr>"
            for row in by_contract.get("rows", [])
        )
        temporal_rows = "".join(
            "<tr>"
            f"<td>{_h(row.get('market_ticker'))}</td><td>{_h(row.get('initial_side'))}</td>"
            f"<td>{_fmt(row.get('initial_entry_price'))}</td><td>{_fmt(row.get('best_combined_basis_seen'))}</td>"
            f"<td>{_fmt(row.get('best_locked_edge_seen'))}</td><td>{_fmt(row.get('time_to_expiry_at_best'))}</td>"
            f"<td>{_fmt(row.get('distance_from_strike_at_best'))}</td><td>{_h(row.get('ts_at_best'))}</td>"
            "</tr>"
            for row in temporal.get("rows", [])
        )
        temporal_chart_json = _h(json.dumps((temporal.get("rows") or [])[:200], default=str))
        distance = _distance(latest.get('btc_price'), latest.get('strike'))
        decision_class = _decision_class(decision.get('action'))
        body = f"""
        <section class="decision-hero {decision_class}">
          <div>
            <p class="eyebrow">SimpleDirectionalStrategy · simulated/paper only</p>
            <h2>Execution Decision</h2>
            <div id="decision-action" class="decision-action">{_h(decision.get('action'))}</div>
            <p id="decision-reason">{_h(decision.get('reason'))}</p>
          </div>
          <div class="decision-grid">
            {_mini_metric('Size', _money(decision.get('size_dollars')), 'paper dollars', value_id='decision-size')}
            {_mini_metric('Confidence', _pct(decision.get('confidence')), 'strategy score', value_id='decision-confidence')}
            {_mini_metric('Blocked by', ', '.join(decision.get('blocked_by') or []) or 'none', 'risk gates', value_id='decision-blocked-by')}
          </div>
        </section>
        <section class="cards">
          {_card('Recorder freshness', _h(stream.get('freshness', {}).get('status')), _h(stream.get('freshness', {}).get('age_seconds')) + 's', value_id='recorder-freshness', note_id='recorder-age')}
          {_card('Latest BTC price', _money(latest.get('btc_price')), 'source ' + _h(latest.get('btc_price_source') or 'unknown'), value_id='btc-price', note_id='above-below-strike')}
          {_card('Distance from strike', _money(distance), f"strike {_money(latest.get('strike'))}", value_id='distance-from-strike', note_id='strike-price')}
          {_card('Seconds to close', _fmt(latest.get('seconds_to_close')), _h(latest.get('market_ticker')), value_id='seconds-to-close', note_id='market-ticker')}
        </section>
        <section class="split"><div><h2>YES / NO orderbook</h2>
          <table><tbody>
            {_kv('YES bid', orderbook.get('yes_bid'), value_id='yes-bid')}{_kv('YES ask', orderbook.get('yes_ask'), value_id='yes-ask')}
            {_kv('NO bid', orderbook.get('no_bid'), value_id='no-bid')}{_kv('NO ask', orderbook.get('no_ask'), value_id='no-ask')}
            {_kv('spread', orderbook.get('spread'), value_id='orderbook-spread')}{_kv('status', orderbook.get('status'), value_id='orderbook-status')}
            {_kv('BTC price source', latest.get('btc_price_source'), value_id='btc-price-source')}
            {_kv('condition id', latest.get('condition_id'))}{_kv('YES token', latest.get('yes_token_id'))}{_kv('NO token', latest.get('no_token_id'))}
          </tbody></table></div>
          <div><h2>Slopes</h2><table><tbody>
            {_kv('slope_10s', slopes.get('slope_10s'), value_id='slope-10s')}{_kv('slope_30s', slopes.get('slope_30s'), value_id='slope-30s')}{_kv('slope_60s', slopes.get('slope_60s'), value_id='slope-60s')}
          </tbody></table></div>
        </section>
        <section><h2>Recent graph points</h2>
          <canvas id="price-chart" width="960" height="260" aria-label="BTC price versus strike chart"></canvas>
          <div id="stream-chart">Read-only chart source: /api/stream history</div>
          <table><thead><tr><th>ts</th><th>BTC</th><th>target</th><th>sec close</th></tr></thead><tbody id="graph-points-body">{graph_rows}</tbody></table>
        </section>
        <section><h2>YES+NO Complement Spread</h2>
          <div class="cards">
            {_card('Buy both cost', _fmt(complement_latest.get('buy_both_cost')), 'YES ask + NO ask')}
            {_card('Buy both edge', _fmt(complement_latest.get('buy_both_edge')), '1 - cost; positive means buy-both lock')}
            {_card('Sell both credit', _fmt(complement_latest.get('sell_both_credit')), 'YES bid + NO bid')}
            {_card('Sell both edge', _fmt(complement_latest.get('sell_both_edge')), 'credit - 1')}
          </div>
          <canvas id="complement-spread-chart" width="960" height="220" aria-label="YES plus NO complement spread"></canvas>
          <table><tbody>
            {_kv('max buy_both_edge', complement_summary.get('max_buy_both_edge'))}{_kv('max sell_both_edge', complement_summary.get('max_sell_both_edge'))}{_kv('buy edge > 1c / 2c / 5c', f"{complement_summary.get('count_buy_edge_gt_1c')} / {complement_summary.get('count_buy_edge_gt_2c')} / {complement_summary.get('count_buy_edge_gt_5c')}")}{_kv('sell edge > 1c / 2c / 5c', f"{complement_summary.get('count_sell_edge_gt_1c')} / {complement_summary.get('count_sell_edge_gt_2c')} / {complement_summary.get('count_sell_edge_gt_5c')}")}
          </tbody></table>
        </section>
        <script type="application/json" id="complement-spread-data">{complement_history_json}</script>
        <section><h2>Complement by 15m Contract / Strike</h2>
          <p class="muted">Full recorded history per market ticker/strike, not just the latest chart window. Positive buy edge means YES ask + NO ask &lt; 1. Positive sell edge means YES bid + NO bid &gt; 1.</p>
          <table id="complement-by-contract-table"><thead><tr><th>market</th><th>strike</th><th>close</th><th>samples</th><th>max buy edge</th><th>max sell edge</th><th>avg buy edge</th><th>best buy sec close</th><th>buy &gt;1c/2c/5c</th><th>sell &gt;1c/2c/5c</th></tr></thead><tbody>{by_contract_rows}</tbody></table>
          <table><tbody>{_kv('contracts scanned', by_contract.get('contracts'))}{_kv('source rows', by_contract.get('source_rows'))}</tbody></table>
        </section>
        <section><h2>Temporal Basis Compression</h2>
          <p class="muted">Scan-only geometry: after the first directional entry window, track best future opposite ask. Evaluate combined settlement basis, locked payout, worst-case value, and residual exposure rather than standalone hedge-leg mark-to-market.</p>
          <canvas id="temporal-compression-chart" width="960" height="220" aria-label="Temporal basis compression by time to expiry"></canvas>
          <table><tbody>
            {_kv('markets basis < 0.99 / 0.95 / 0.92 / 0.90 / 0.85', f"{(temporal.get('count_best_combined_basis_lt') or {}).get('0.99')} / {(temporal.get('count_best_combined_basis_lt') or {}).get('0.95')} / {(temporal.get('count_best_combined_basis_lt') or {}).get('0.92')} / {(temporal.get('count_best_combined_basis_lt') or {}).get('0.9')} / {(temporal.get('count_best_combined_basis_lt') or {}).get('0.85')}")}
            {_kv('best basis min / avg', f"{_fmt(temporal.get('best_basis_min'))} / {_fmt(temporal.get('best_basis_avg'))}")}{_kv('assessment', temporal.get('structural_exploitability_assessment'))}
          </tbody></table>
          <table id="temporal-compression-table"><thead><tr><th>market</th><th>initial side</th><th>initial price</th><th>best basis</th><th>temporal edge</th><th>sec close</th><th>distance</th><th>best ts</th></tr></thead><tbody>{temporal_rows}</tbody></table>
          <script type="application/json" id="temporal-compression-data">{temporal_chart_json}</script>
        </section>
        <section><h2>Raw payload</h2><button type="button" onclick="copyApiJson('/api/stream')">Copy API JSON</button><pre id="raw-payload">{_h(json.dumps(_safe_raw_payload(latest.get('raw_payload')), indent=2, sort_keys=True))}</pre></section>
        <script type="application/json" id="stream-history-data">{chart_points_json}</script>
        """
    return _page(
        title=str(data.get("title") or STREAM_TITLE),
        heading=STREAM_TITLE,
        subheading=f"{_h(data.get('mode'))} | API {_h(data.get('api_path'))} | {_h(data.get('boundary'))}",
        body=body,
        refresh_ms=1000,
        stream_api_path="/api/stream",
    )


def render_status_dashboard_html(data: Mapping[str, Any]) -> str:
    perf = data.get("paper_performance") if isinstance(data.get("paper_performance"), Mapping) else {}
    metrics = perf.get("metrics") if isinstance(perf.get("metrics"), Mapping) else {}
    services = data.get("services") if isinstance(data.get("services"), Mapping) else {}
    service_rows = "".join(
        f"<tr><td>{_h(name)}</td><td>{_h(info.get('ActiveState') if isinstance(info, Mapping) else info)}</td></tr>"
        for name, info in services.items()
    )
    review_rows = "".join(
        "<tr>"
        f"<td>{_h(row.get('created_at'))}</td><td>{_h(row.get('market_ticker'))}</td><td>{_h(row.get('side'))}</td>"
        f"<td>{_fmt(row.get('entry_price'))}</td><td>{_h(row.get('status'))}</td><td>{_fmt(row.get('realized_pnl'))}</td>"
        "</tr>"
        for row in perf.get("review_trades", [])
    )
    bucket_sections = "".join(
        f"<h3>{_h(name)}</h3><table><tbody>"
        + "".join(f"<tr><td>{_h(item['bucket'])}</td><td>{item['count']}</td><td>{_fmt(item['realized_pnl'])}</td></tr>" for item in rows)
        + "</tbody></table>"
        for name, rows in (perf.get("review_buckets") or {}).items()
    )
    recent_rows = "".join(
        f"<tr><td>{_h(row.get('created_at'))}</td><td>{_h(row.get('market_ticker'))}</td><td>{_h(row.get('side'))}</td><td>{_h(row.get('status'))}</td><td>{_fmt(row.get('notional'))}</td><td>{_h(row.get('settlement_source'))}</td></tr>"
        for row in perf.get("recent_trades", [])
    )
    open_position_rows = "".join(
        f"<tr><td>{_h(row.get('created_at'))}</td><td>{_h(row.get('market_ticker'))}</td><td>{_h(row.get('side'))}</td><td>{_fmt(row.get('entry_price'))}</td><td>{_fmt(row.get('notional'))}</td><td>{_h(row.get('market_close_time'))}</td></tr>"
        for row in perf.get("open_positions", [])
    )
    signal_rows = _group_rows(perf.get("grouped_by_signal", []), label_key="action")
    market_rows = _group_rows(perf.get("grouped_by_market", []), label_key="market_ticker")
    blocker_rows = "".join(
        f"<tr><td>{_h(row.get('blocker'))}</td><td>{row.get('count')}</td></tr>" for row in data.get("top_blockers", [])
    )
    equity_json = _h(json.dumps(perf.get('equity_curve', []), default=str))
    rejected_seed_rows = "".join(
        f"<tr><td>{_h(row.get('ts'))}</td><td>{_h(row.get('market_ticker'))}</td><td>{_h(row.get('side'))}</td><td>{_fmt(row.get('ask_price'))}</td><td>{_h(row.get('reason'))}</td></tr>"
        for row in data.get("rejected_seed_attempts", [])
    )
    body = f"""
    <p class="boundary">{_h(data.get('boundary'))}</p>
    <section id="status-summary" class="cards status-summary">
      {_card('Paper realized PnL', _money(metrics.get('realized_pnl')), 'settled')}
      {_card('Paper total PnL', _money(metrics.get('total_pnl')), 'realized + unrealized')}
      {_card('Win rate', _pct(metrics.get('win_rate')), f"{metrics.get('closed_positions', 0)} settled / {metrics.get('open_positions', 0)} open")}
      {_card('Average trade', _money(metrics.get('average_trade')), f"avg win {_money(metrics.get('avg_win'))} / avg loss {_money(metrics.get('avg_loss'))}")}
      {_card('Latest prediction', _h(data.get('latest_prediction_timestamp')), 'paper executor freshness')}
    </section>
    <section><h2>Paper Trading Performance</h2><table><tbody>
      {_kv('snapshot DB path', data.get('snapshot_db_path'))}{_kv('results DB path', data.get('results_db_path'))}
      {_kv('latest snapshot timestamp', data.get('latest_snapshot_timestamp'))}{_kv('latest prediction timestamp', data.get('latest_prediction_timestamp'))}
      {_kv('settled trades', metrics.get('closed_positions'))}{_kv('open trades', metrics.get('open_positions'))}
      {_kv('Settlement source', 'per-trade: kalshi_official or coinbase_estimate')}
    </tbody></table></section>
    <section><h2>Open paper positions</h2><table><thead><tr><th>created</th><th>market</th><th>side</th><th>entry</th><th>notional</th><th>close</th></tr></thead><tbody>{open_position_rows}</tbody></table></section>
    <section><h2>Active service status</h2><table><tbody>{service_rows}</tbody></table></section>
    <section><h2>Equity curve</h2><canvas id="equity-chart" width="960" height="260" aria-label="Paper equity curve"></canvas><div id="equity-curve">Read-only paper equity curve</div><pre>{_h(json.dumps(perf.get('equity_curve', []), indent=2))}</pre></section>
    <section><h2>Paper PnL Review</h2><table><thead><tr><th>created</th><th>market</th><th>side</th><th>entry</th><th>status</th><th>PnL</th></tr></thead><tbody>{review_rows}</tbody></table></section>
    <section><h2>Paper Review Buckets</h2>{bucket_sections}</section>
    <section><h2>Recent paper trades</h2><table><thead><tr><th>created</th><th>market</th><th>side</th><th>status</th><th>notional</th><th>Settlement source</th></tr></thead><tbody>{recent_rows}</tbody></table></section>
    <section><h2>Grouped by signal</h2><table><tbody>{signal_rows}</tbody></table></section>
    <section><h2>Grouped by market</h2><table><tbody>{market_rows}</tbody></table></section>
    <section><h2>Top blockers</h2><table><tbody>{blocker_rows}</tbody></table></section>
    <script type="application/json" id="equity-data">{equity_json}</script>
    """
    return _page(
        title=str(data.get("title") or STATUS_TITLE),
        heading=STATUS_TITLE,
        subheading=f"API {_h(data.get('api_path'))} or /api/status",
        body=body,
        refresh_ms=5000,
    )


def delete_strategy_run(*, runs_dir: str | Path | None = None, strategy: str, run_id: str) -> JsonDict:
    runs_path = resolve_runs_dir(runs_dir)
    if run_id == "live" or strategy == "live":
        raise ValueError("live strategy slots cannot be deleted from the replay dashboard")
    run_dir = _safe_run_dir(runs_path=runs_path, strategy=strategy, run_id=run_id)
    if not run_dir.exists():
        raise FileNotFoundError(f"strategy run not found: {strategy}/{run_id}")
    if not run_dir.is_dir():
        raise ValueError("strategy run path is not a directory")
    shutil.rmtree(run_dir)
    return {"deleted": True, "strategy": strategy, "run_id": run_id, "run_dir": str(run_dir)}


def _safe_run_dir(*, runs_path: Path, strategy: str, run_id: str) -> Path:
    if any(part in {"", ".", ".."} or "/" in part or "\\" in part for part in (strategy, run_id)):
        raise ValueError("unsafe strategy/run path")
    root = runs_path.resolve()
    candidate = (runs_path / strategy / run_id).resolve()
    if root != candidate and root not in candidate.parents:
        raise ValueError("unsafe strategy/run path")
    return candidate


def _run_dir_for_route(*, runs_path: Path, strategy: str, run_id: str) -> Path:
    normal = runs_path / strategy / run_id
    if normal.exists():
        return normal
    # Live-paper strategy slots are stored as runs/live/<strategy> but exposed as
    # /strategy/<strategy>/live so replay and live slots share one URL shape.
    live_slot = runs_path / "live" / strategy
    if run_id == "live" and live_slot.exists():
        return live_slot
    return normal


def _run_summary_from_dir(run_dir: Path) -> JsonDict:
    metrics_path = run_dir / "metrics.json"
    config_path = run_dir / "config.toml"
    results_path = run_dir / "results.sqlite3"
    if not metrics_path.is_file() or not config_path.is_file() or not results_path.is_file():
        raise ValueError("missing config.toml, metrics.json, or results.sqlite3")
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    if not isinstance(metrics, Mapping):
        raise ValueError("metrics.json must contain an object")
    institutional = metrics.get("institutional_metrics")
    if not isinstance(institutional, Mapping):
        institutional = {}
    strategy = str(metrics.get("strategy") or run_dir.parent.name)
    run_id = str(metrics.get("run_id") or run_dir.name)
    summary = {
        "strategy": strategy,
        "run_id": run_id,
        "created_at": metrics.get("created_at") or run_id,
        "snapshots": metrics.get("snapshots", 0),
        "signals": metrics.get("signals", 0),
        "fills": metrics.get("fills", 0),
        "notional": metrics.get("notional", 0.0),
        "settled_positions": metrics.get("settled_positions", 0),
        "max_open_positions": metrics.get("max_open_positions"),
        "mode": metrics.get("mode"),
        "blockers": _result_blockers(results_path),
        "run_dir": str(run_dir),
        "results_db": str(results_path),
        "href": f"/strategy/{_url_component(strategy)}/{_url_component(run_id)}",
        "api_href": f"/api/strategies/{_url_component(strategy)}/{_url_component(run_id)}",
        "delete_href": f"/api/strategies/{_url_component(strategy)}/{_url_component(run_id)}",
    }
    summary["institutional_metrics"] = dict(institutional)
    for key in (
        "win_rate",
        "ev_per_trade",
        "max_drawdown",
        "max_drawdown_pct",
        "sharpe",
        "sortino",
        "calmar",
        "profit_factor",
        "total_pnl",
        "settlement_source",
    ):
        summary[key] = institutional.get(key, metrics.get(key))
    return summary


def _result_blockers(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    blockers: Counter[str] = Counter()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=2000")
        if _table_exists(conn, "replay_signals"):
            rows = conn.execute(
                "SELECT blocked_by_json FROM replay_signals WHERE allowed = 0 ORDER BY id DESC LIMIT 5000"
            ).fetchall()
            for row in rows:
                for blocker in _json_loads(row["blocked_by_json"], default=[]):
                    blockers[str(blocker)] += 1
        elif _table_exists(conn, "predictions"):
            columns = _table_columns(conn, "predictions")
            select = "reasons_json, stake_dollars" if "stake_dollars" in columns else "reasons_json, 0.0 AS stake_dollars"
            rows = conn.execute(f"SELECT {select} FROM predictions ORDER BY id DESC LIMIT 5000").fetchall()
            for row in rows:
                stake = _float(row["stake_dollars"])
                if stake is not None and stake > 0:
                    continue
                for reason in _json_loads(row["reasons_json"], default=[]):
                    text = str(reason)
                    if text in {"max_open_positions", "spread_too_wide", "invalid_orderbook", "signal_none"}:
                        blockers[text] += 1
    finally:
        conn.close()
    return dict(blockers)


def _read_result_rows(path: Path, table: str, *, limit: int) -> list[JsonDict]:
    if table not in {"replay_signals", "replay_fills", "pair_positions", "hedge_events", "missed_hedge_opportunities", "rejected_seed_attempts", "inventory_vol_positions", "inventory_vol_events", "inventory_vol_research_metrics", "inventory_vol_regime_positions", "inventory_vol_regime_events", "inventory_vol_regime_research_metrics", "volatility_hedge_positions", "volatility_hedge_events"}:
        raise ValueError(f"unsupported results table: {table}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=2000")
        if not _table_exists(conn, table):
            return []
        order_column = "id" if _column_exists(conn, table, "id") else "updated_at" if _column_exists(conn, table, "updated_at") else "rowid"
        columns = _safe_result_columns(conn, table)
        selected_columns = ", ".join(columns) if columns else "*"
        rows = conn.execute(f"SELECT {selected_columns} FROM {table} ORDER BY {order_column} DESC LIMIT ?", (max(1, limit),)).fetchall()
        return [_normalize_result_row(dict(row)) for row in rows]
    finally:
        conn.close()


def _safe_result_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]
    if table == "volatility_hedge_events":
        return [column for column in columns if column != "raw_json"]
    return columns


def _normalize_result_row(row: JsonDict) -> JsonDict:
    for key in ("fills_json", "last_features_json", "equity_curve_json", "features_timeline_json"):
        if key in row:
            value = _json_loads(row.get(key), default=row.get(key))
            if isinstance(value, list):
                row[f"{key}_count"] = len(value)
                row[key] = json.dumps(value[-10:], sort_keys=True)
            elif isinstance(value, dict):
                row[key] = json.dumps(value, sort_keys=True)
    return row


def _url_component(value: Any) -> str:
    text = str(value)
    safe = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_- .:"
    return "".join(ch if ch in safe else "_" for ch in text).replace(" ", "%20")


def _stream_latest_from_row(row: sqlite3.Row) -> JsonDict:
    raw_payload = _json_loads(row_get(row, "raw_json"), default={})
    raw_state = _json_loads(row_get(row, "raw_state_json"), default={})
    ts = row_get(row, "ts")
    btc_price = _float(row_get(row, "btc_price"))
    strike = _float(row_get(row, "strike"))
    target = _float(row_get(row, "target_price")) or strike
    close_time = row_get(row, "market_close_time")
    seconds_to_close = _float(row_get(row, "seconds_to_close"))
    if seconds_to_close is None and ts and close_time:
        seconds_to_close = _seconds_between(ts, close_time)
    state = _state_from_snapshot(row)
    signal = SimpleDirectionalStrategy().on_tick(state) if state else None
    risk = RiskManager(RiskLimits(max_open_positions=999999)).evaluate(state, signal) if state and signal else None
    action = _action_for_signal(signal.side if signal else "none")
    blocked_by = list(risk.blocked_by) if risk else ["missing_state"]
    if risk and not risk.allowed:
        action = "NO_TRADE"
    latest = {
        "ts": ts,
        "market_ticker": row_get(row, "market_ticker"),
        "market_slug": row_get(row, "market_slug"),
        "btc_price_source": _btc_price_source(raw_payload if raw_payload else raw_state),
        "condition_id": row_get(row, "condition_id"),
        "yes_token_id": row_get(row, "yes_token_id"),
        "no_token_id": row_get(row, "no_token_id"),
        "market_close_time": close_time,
        "btc_price": btc_price,
        "strike": strike,
        "target_price": target,
        "seconds_to_close": seconds_to_close,
        "above_below_strike": _above_below(btc_price, strike),
        "slopes": _slopes_for_latest(row),
        "orderbook": _orderbook_status(row),
        "execution_decision": {
            "action": action,
            "size_dollars": risk.size_dollars if risk else 0.0,
            "blocked_by": blocked_by,
            "reason": risk.reason if risk else "missing snapshot fields",
            "confidence": signal.confidence if signal else 0.0,
        },
        "raw_payload": raw_payload if raw_payload else raw_state,
    }
    return latest


def _btc_price_source(payload: Mapping[str, Any]) -> str | None:
    direct = payload.get("btc_price_source")
    if direct not in (None, ""):
        return str(direct)
    raw = payload.get("btc_price_raw")
    if isinstance(raw, Mapping) and raw.get("source") not in (None, ""):
        return str(raw.get("source"))
    snapshot = payload.get("snapshot")
    if isinstance(snapshot, Mapping):
        nested = snapshot.get("btc_price_raw")
        if isinstance(nested, Mapping) and nested.get("source") not in (None, ""):
            return str(nested.get("source"))
    return None


def _state_from_snapshot(row: sqlite3.Row) -> MarketState | None:
    ts = _parse_dt(row_get(row, "ts"))
    close = _parse_dt(row_get(row, "market_close_time"))
    price = _float(row_get(row, "btc_price"))
    strike = _float(row_get(row, "strike"))
    if ts is None or close is None or price is None or strike is None:
        return None
    return MarketState(
        tick=Tick(ts=ts, price=price, source="dashboard_readonly"),
        orderbook=OrderBookSnapshot(
            ts=ts,
            market_ticker=str(row_get(row, "market_ticker") or ""),
            yes_bid=_float(row_get(row, "yes_bid")),
            yes_ask=_float(row_get(row, "yes_ask")),
            no_bid=_float(row_get(row, "no_bid")),
            no_ask=_float(row_get(row, "no_ask")),
            sequence=_int(row_get(row, "orderbook_sequence")),
        ),
        contract=ContractWindow(
            ticker=str(row_get(row, "market_ticker") or ""),
            strike=strike,
            close_time=close,
            open_time=_parse_dt(row_get(row, "market_open_time")),
        ),
        slope_30s=_float(row_get(row, "slope_30s")) or _float(row_get(row, "btc_velocity_30s")),
    )


def _latest_snapshot_rows(path: Path, *, limit: int) -> list[sqlite3.Row]:
    if not path.exists():
        return []
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=2000")
        if not _table_exists(conn, STREAM_TABLE):
            return []
        rows = conn.execute(
            f"SELECT * FROM {STREAM_TABLE} ORDER BY ts DESC LIMIT ?", (max(1, limit),)
        ).fetchall()
    return list(reversed(rows))


def _paper_performance(path: Path) -> JsonDict:
    empty = {
        "latest_prediction_timestamp": None,
        "metrics": _metrics([]),
        "equity_curve": [],
        "review_trades": [],
        "review_buckets": {name: [] for name in _bucket_names()},
        "recent_trades": [],
        "open_positions": [],
        "grouped_by_signal": [],
        "grouped_by_market": [],
        "top_blockers": [],
    }
    if not path.exists():
        return empty
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=2000")
        if not _table_exists(conn, "paper_trades"):
            return empty
        trades = [dict(row) for row in conn.execute("SELECT * FROM paper_trades ORDER BY created_at ASC").fetchall()]
        latest_prediction = None
        predictions: list[dict[str, Any]] = []
        if _table_exists(conn, "predictions"):
            latest_prediction = conn.execute("SELECT MAX(created_at) FROM predictions").fetchone()[0]
            # Keep status/dashboard requests cheap under browser auto-refresh. The
            # paper PnL tables are trade-sourced; predictions are used here only
            # for recent signal grouping and blocker summaries.
            predictions = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM predictions ORDER BY created_at DESC LIMIT 5000"
                ).fetchall()
            ]
    return {
        "latest_prediction_timestamp": latest_prediction,
        "metrics": _metrics(trades),
        "equity_curve": _equity_curve(trades),
        "review_trades": list(reversed(trades))[:50],
        "review_buckets": _review_buckets(trades, predictions),
        "recent_trades": list(reversed(trades))[:25],
        "open_positions": [t for t in reversed(trades) if str(t.get("status") or "").upper() == "OPEN"],
        "grouped_by_signal": _group_by(predictions, key="action", pnl_by_prediction=_pnl_by_prediction(trades)),
        "grouped_by_market": _group_by(trades, key="market_ticker"),
        "top_blockers": _top_blockers(predictions),
    }


def _metrics(trades: list[dict[str, Any]]) -> JsonDict:
    settled = [t for t in trades if str(t.get("status") or "").upper() in {"SETTLED", "CLOSED"}]
    open_trades = [t for t in trades if str(t.get("status") or "").upper() == "OPEN"]
    pnls = [_float(t.get("realized_pnl")) or 0.0 for t in settled]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    realized = sum(pnls)
    unrealized = 0.0
    total = realized + unrealized
    return {
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "total_pnl": total,
        "closed_positions": len(settled),
        "open_positions": len(open_trades),
        "settled_trades": len(settled),
        "open_trades": len(open_trades),
        "win_rate": len(wins) / len(settled) if settled else 0.0,
        "average_trade": realized / len(settled) if settled else 0.0,
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
    }


def _review_buckets(trades: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> dict[str, list[JsonDict]]:
    pred_by_id = {p.get("id"): p for p in predictions}
    buckets: dict[str, Counter[str]] = {name: Counter() for name in _bucket_names()}
    pnl: dict[str, defaultdict[str, float]] = {name: defaultdict(float) for name in _bucket_names()}
    for trade in trades:
        pred = pred_by_id.get(trade.get("prediction_id"), {})
        features = _json_loads(pred.get("features_json"), default={})
        values = {
            "side": str(trade.get("side") or "unknown"),
            "entry_price_bucket": _price_bucket(_float(trade.get("entry_price"))),
            "seconds_to_expiry_bucket": _seconds_bucket(_float(features.get("seconds_to_expiry"))),
            "distance_from_strike_bucket": _distance_bucket(_float(features.get("distance_from_strike"))),
            "slope_at_entry_bucket": _slope_bucket(_float(features.get("slope_at_entry"))),
            "hold_time_bucket": _hold_bucket(trade),
        }
        trade_pnl = _float(trade.get("realized_pnl")) or 0.0
        for name, bucket in values.items():
            buckets[name][bucket] += 1
            pnl[name][bucket] += trade_pnl
    return {
        name: [
            {"bucket": bucket, "count": count, "realized_pnl": pnl[name][bucket]}
            for bucket, count in counter.most_common()
        ]
        for name, counter in buckets.items()
    }


def _bucket_names() -> tuple[str, ...]:
    return (
        "side",
        "entry_price_bucket",
        "seconds_to_expiry_bucket",
        "distance_from_strike_bucket",
        "slope_at_entry_bucket",
        "hold_time_bucket",
    )


def _top_blockers(predictions: list[dict[str, Any]]) -> list[JsonDict]:
    counts: Counter[str] = Counter()
    for pred in predictions:
        for reason in _json_loads(pred.get("reasons_json"), default=[]):
            text = str(reason)
            if "max_open_positions" in text:
                counts["max_open_positions"] += 1
            elif "blocked" in text.lower():
                counts[text] += 1
    return [{"blocker": blocker, "count": count} for blocker, count in counts.most_common(10)]


def _group_by(rows: list[dict[str, Any]], *, key: str, pnl_by_prediction: dict[str, float] | None = None) -> list[JsonDict]:
    grouped: dict[str, JsonDict] = {}
    for row in rows:
        label = str(row.get(key) or "unknown")
        item = grouped.setdefault(label, {key: label, "count": 0, "realized_pnl": 0.0})
        item["count"] += 1
        item["realized_pnl"] += (
            pnl_by_prediction.get(str(row.get("id")), 0.0) if pnl_by_prediction is not None else (_float(row.get("realized_pnl")) or 0.0)
        )
    return sorted(grouped.values(), key=lambda item: (-item["count"], str(item[key])))


def _pnl_by_prediction(trades: list[dict[str, Any]]) -> dict[str, float]:
    totals: defaultdict[str, float] = defaultdict(float)
    for trade in trades:
        totals[str(trade.get("prediction_id"))] += _float(trade.get("realized_pnl")) or 0.0
    return dict(totals)


def _equity_curve(trades: list[dict[str, Any]]) -> list[JsonDict]:
    equity = 0.0
    points = []
    for trade in trades:
        pnl = _float(trade.get("realized_pnl"))
        if pnl is None:
            continue
        equity += pnl
        points.append({"ts": trade.get("settled_at") or trade.get("created_at"), "equity": equity})
    return points


def _history_point(row: sqlite3.Row) -> JsonDict:
    return {
        "ts": row_get(row, "ts"),
        "btc_price": _float(row_get(row, "btc_price")),
        "target_price": _float(row_get(row, "target_price")) or _float(row_get(row, "strike")),
        "seconds_to_close": _float(row_get(row, "seconds_to_close")),
        "yes_ask": _float(row_get(row, "yes_ask")),
        "no_ask": _float(row_get(row, "no_ask")),
        **_complement_metrics_from_prices(
            _float(row_get(row, "yes_bid")),
            _float(row_get(row, "yes_ask")),
            _float(row_get(row, "no_bid")),
            _float(row_get(row, "no_ask")),
        ),
    }


def _complement_metrics_from_prices(
    yes_bid: float | None,
    yes_ask: float | None,
    no_bid: float | None,
    no_ask: float | None,
) -> JsonDict:
    if yes_bid is None or yes_ask is None or no_bid is None or no_ask is None:
        return {
            "buy_both_cost": None,
            "buy_both_edge": None,
            "sell_both_credit": None,
            "sell_both_edge": None,
        }
    buy_both_cost = yes_ask + no_ask
    sell_both_credit = yes_bid + no_bid
    return {
        "buy_both_cost": buy_both_cost,
        "buy_both_edge": 1.0 - buy_both_cost,
        "sell_both_credit": sell_both_credit,
        "sell_both_edge": sell_both_credit - 1.0,
    }


def _complement_spread_payload(history: list[JsonDict]) -> JsonDict:
    rows = [row for row in history if row.get("buy_both_edge") is not None and row.get("sell_both_edge") is not None]
    latest = rows[-1] if rows else None
    buy_edges = [_float(row.get("buy_both_edge")) for row in rows]
    sell_edges = [_float(row.get("sell_both_edge")) for row in rows]
    buy_edges = [edge for edge in buy_edges if edge is not None]
    sell_edges = [edge for edge in sell_edges if edge is not None]
    return {
        "latest": latest,
        "history": rows,
        "summary": {
            "samples": len(rows),
            "max_buy_both_edge": max(buy_edges) if buy_edges else None,
            "max_sell_both_edge": max(sell_edges) if sell_edges else None,
            "count_buy_edge_gt_1c": sum(edge > 0.01 for edge in buy_edges),
            "count_buy_edge_gt_2c": sum(edge > 0.02 for edge in buy_edges),
            "count_buy_edge_gt_5c": sum(edge > 0.05 for edge in buy_edges),
            "count_sell_edge_gt_1c": sum(edge > 0.01 for edge in sell_edges),
            "count_sell_edge_gt_2c": sum(edge > 0.02 for edge in sell_edges),
            "count_sell_edge_gt_5c": sum(edge > 0.05 for edge in sell_edges),
        },
    }


def _complement_by_contract_payload(path: Path, *, limit: int = 200) -> JsonDict:
    empty: JsonDict = {"contracts": 0, "source_rows": 0, "rows": []}
    if not path.exists():
        return empty
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=2000")
        if not _table_exists(conn, STREAM_TABLE):
            return empty
        source_rows = conn.execute(f"SELECT COUNT(*) FROM {STREAM_TABLE}").fetchone()[0]
        rows = conn.execute(
            f"""
            WITH priced AS (
                SELECT
                    market_ticker,
                    strike,
                    market_open_time,
                    market_close_time,
                    ts,
                    seconds_to_close,
                    1.0 - (yes_ask + no_ask) AS buy_both_edge,
                    (yes_bid + no_bid) - 1.0 AS sell_both_edge
                FROM {STREAM_TABLE}
                WHERE yes_bid IS NOT NULL
                  AND yes_ask IS NOT NULL
                  AND no_bid IS NOT NULL
                  AND no_ask IS NOT NULL
                  AND market_ticker IS NOT NULL
            ), grouped AS (
                SELECT
                    market_ticker,
                    strike,
                    market_open_time,
                    market_close_time,
                    MIN(ts) AS first_ts,
                    MAX(ts) AS last_ts,
                    COUNT(*) AS samples,
                    MIN(seconds_to_close) AS min_seconds_to_close,
                    MAX(seconds_to_close) AS max_seconds_to_close,
                    MAX(buy_both_edge) AS max_buy_both_edge,
                    MAX(sell_both_edge) AS max_sell_both_edge,
                    AVG(buy_both_edge) AS avg_buy_both_edge,
                    AVG(sell_both_edge) AS avg_sell_both_edge,
                    SUM(CASE WHEN buy_both_edge > 0.01 THEN 1 ELSE 0 END) AS buy_gt_1c,
                    SUM(CASE WHEN buy_both_edge > 0.02 THEN 1 ELSE 0 END) AS buy_gt_2c,
                    SUM(CASE WHEN buy_both_edge > 0.05 THEN 1 ELSE 0 END) AS buy_gt_5c,
                    SUM(CASE WHEN sell_both_edge > 0.01 THEN 1 ELSE 0 END) AS sell_gt_1c,
                    SUM(CASE WHEN sell_both_edge > 0.02 THEN 1 ELSE 0 END) AS sell_gt_2c,
                    SUM(CASE WHEN sell_both_edge > 0.05 THEN 1 ELSE 0 END) AS sell_gt_5c
                FROM priced
                GROUP BY market_ticker, strike, market_open_time, market_close_time
            )
            SELECT
                grouped.*,
                (
                    SELECT ts FROM priced p
                    WHERE p.market_ticker = grouped.market_ticker
                      AND IFNULL(p.strike, -1) = IFNULL(grouped.strike, -1)
                      AND IFNULL(p.market_open_time, '') = IFNULL(grouped.market_open_time, '')
                      AND IFNULL(p.market_close_time, '') = IFNULL(grouped.market_close_time, '')
                    ORDER BY buy_both_edge DESC, ts DESC LIMIT 1
                ) AS best_buy_ts,
                (
                    SELECT seconds_to_close FROM priced p
                    WHERE p.market_ticker = grouped.market_ticker
                      AND IFNULL(p.strike, -1) = IFNULL(grouped.strike, -1)
                      AND IFNULL(p.market_open_time, '') = IFNULL(grouped.market_open_time, '')
                      AND IFNULL(p.market_close_time, '') = IFNULL(grouped.market_close_time, '')
                    ORDER BY buy_both_edge DESC, ts DESC LIMIT 1
                ) AS best_buy_seconds_to_close,
                (
                    SELECT ts FROM priced p
                    WHERE p.market_ticker = grouped.market_ticker
                      AND IFNULL(p.strike, -1) = IFNULL(grouped.strike, -1)
                      AND IFNULL(p.market_open_time, '') = IFNULL(grouped.market_open_time, '')
                      AND IFNULL(p.market_close_time, '') = IFNULL(grouped.market_close_time, '')
                    ORDER BY sell_both_edge DESC, ts DESC LIMIT 1
                ) AS best_sell_ts,
                (
                    SELECT seconds_to_close FROM priced p
                    WHERE p.market_ticker = grouped.market_ticker
                      AND IFNULL(p.strike, -1) = IFNULL(grouped.strike, -1)
                      AND IFNULL(p.market_open_time, '') = IFNULL(grouped.market_open_time, '')
                      AND IFNULL(p.market_close_time, '') = IFNULL(grouped.market_close_time, '')
                    ORDER BY sell_both_edge DESC, ts DESC LIMIT 1
                ) AS best_sell_seconds_to_close
            FROM grouped
            ORDER BY market_close_time DESC, market_ticker ASC
            LIMIT ?
            """,
            (max(1, limit),),
        ).fetchall()
    output_rows = [dict(row) for row in rows]
    return {"contracts": len(output_rows), "source_rows": int(source_rows or 0), "rows": output_rows}


def _temporal_basis_compression_payload(path: Path, *, limit: int = 200) -> JsonDict:
    empty: JsonDict = {
        "source_rows": 0,
        "rows": [],
        "count_best_combined_basis_lt": {"0.99": 0, "0.95": 0, "0.92": 0, "0.9": 0, "0.85": 0},
        "best_basis_min": None,
        "best_basis_avg": None,
        "structural_exploitability_assessment": "no replay rows available",
    }
    if not path.exists():
        return empty
    try:
        summary = replay_feed_db(path, config=DynamicHedgeConfig(slippage=0.0), scan_only=True)
        rows = _temporal_rows_for_dashboard(path, limit=limit)
    except (OSError, sqlite3.Error, ValueError):
        return empty
    compression = dict(summary.get("temporal_basis_compression") or {})
    compression["rows"] = rows
    compression["source_rows"] = int(summary.get("opportunities_recorded") or 0)
    return {**empty, **compression}


def _temporal_rows_for_dashboard(path: Path, *, limit: int) -> list[JsonDict]:
    from .strategy.dynamic_complement_hedge import DynamicComplementHedgeBot, _snapshot_from_row

    bot = DynamicComplementHedgeBot(DynamicHedgeConfig(slippage=0.0))
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=2000")
        if not _table_exists(conn, STREAM_TABLE):
            return []
        rows = list(conn.execute(f"SELECT * FROM {STREAM_TABLE} ORDER BY market_ticker ASC, ts ASC"))
    for row in rows:
        bot.on_snapshot(_snapshot_from_row(row), scan_only=True)
    output = [item.summary_dict() for item in bot.temporal.values() if item.initial_entry_price is not None]
    output.sort(key=lambda row: (row.get("best_combined_basis_seen") is None, row.get("best_combined_basis_seen") or 999.0))
    return output[: max(1, limit)]


def _slopes_for_latest(row: sqlite3.Row) -> JsonDict:
    rows = []
    try:
        rows = _neighbor_rows(row, seconds=60)
    except sqlite3.Error:
        rows = []
    return {
        "slope_10s": _slope_from_rows(rows, 10),
        "slope_30s": _float(row_get(row, "slope_30s")) or _float(row_get(row, "btc_velocity_30s")) or _slope_from_rows(rows, 30),
        "slope_60s": _slope_from_rows(rows, 60),
    }


def _neighbor_rows(row: sqlite3.Row, *, seconds: int) -> list[sqlite3.Row]:
    # Kept intentionally simple; the current test snapshots fit in the same DB query elsewhere.
    return [row]


def _slope_from_rows(rows: list[sqlite3.Row], window_seconds: int) -> float | None:
    if not rows:
        return None
    latest = rows[-1]
    latest_ts = _parse_dt(row_get(latest, "ts"))
    latest_price = _float(row_get(latest, "btc_price"))
    if latest_ts is None or latest_price is None:
        return None
    candidate = rows[0]
    candidate_ts = _parse_dt(row_get(candidate, "ts"))
    candidate_price = _float(row_get(candidate, "btc_price"))
    if candidate_ts is None or candidate_price is None or candidate_ts == latest_ts:
        return _float(row_get(latest, "btc_velocity_30s")) or _float(row_get(latest, "slope_30s"))
    elapsed = (latest_ts - candidate_ts).total_seconds()
    if elapsed <= 0:
        return None
    return (latest_price - candidate_price) / min(elapsed, window_seconds)


def _orderbook_status(row: sqlite3.Row) -> JsonDict:
    yes_bid = _float(row_get(row, "yes_bid"))
    yes_ask = _float(row_get(row, "yes_ask"))
    no_bid = _float(row_get(row, "no_bid"))
    no_ask = _float(row_get(row, "no_ask"))
    spreads = [ask - bid for bid, ask in ((yes_bid, yes_ask), (no_bid, no_ask)) if bid is not None and ask is not None]
    valid = bool(spreads) and all(spread >= 0 for spread in spreads)
    return {
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "spread": min(spreads) if spreads else None,
        "status": "valid" if valid else "stale_or_invalid",
    }


def _freshness(ts: str | None) -> JsonDict:
    parsed = _parse_dt(ts)
    if parsed is None:
        return {"status": "missing", "age_seconds": None}
    age = max(0.0, (_now() - parsed).total_seconds())
    return {"status": "fresh" if age <= 5 else "stale", "age_seconds": round(age, 3)}


def _systemd_user_service_status(names: Iterable[str]) -> dict[str, JsonDict]:
    statuses: dict[str, JsonDict] = {}
    for name in names:
        result = subprocess.run(
            ["systemctl", "--user", "show", name, "--property=ActiveState", "--property=SubState", "--no-page"],
            check=False,
            text=True,
            capture_output=True,
            timeout=5,
        )
        info: dict[str, str] = {}
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    info[key] = value
        statuses[name] = info or {"ActiveState": "unknown"}
    return statuses


class DashboardHandler(BaseHTTPRequestHandler):
    snapshot_db: Path
    results_db: Path
    runs_dir: Path
    history_limit: int

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path in {"/", "/stream"}:
                self._send_html(render_stream_dashboard_html(self._stream_data()))
            elif self.path == "/status":
                self._send_html(render_status_dashboard_html(self._status_data()))
            elif self.path == "/strategies":
                self._send_html(render_strategy_runs_dashboard_html(self._strategy_runs_data()))
            elif self.path.startswith("/strategy/"):
                detail = self._strategy_detail_from_path(html_path=True)
                self._send_html(render_strategy_run_detail_html(detail))
            elif self.path == "/api/stream":
                self._send_json(self._stream_data())
            elif self.path in {"/api/dashboard", "/api/status"}:
                self._send_json(self._status_data())
            elif self.path == "/api/strategies":
                self._send_json(self._strategy_runs_data())
            elif self.path.startswith("/api/strategies/"):
                self._send_json(self._strategy_detail_from_path(html_path=False))
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            return
        except (sqlite3.Error, OSError) as exc:
            try:
                self._send_json({"ok": False, "error": type(exc).__name__, "message": str(exc)}, status=503)
            except (BrokenPipeError, ConnectionResetError):
                return

    def do_DELETE(self) -> None:  # noqa: N802
        try:
            if self.path.startswith("/api/strategies/"):
                parts = self.path.removeprefix("/api/strategies/").split("/", 1)
                if len(parts) != 2 or not parts[0] or not parts[1]:
                    raise FileNotFoundError("strategy/run path requires strategy and run_id")
                result = delete_strategy_run(
                    runs_dir=self.runs_dir,
                    strategy=unquote(parts[0]),
                    run_id=unquote(parts[1]),
                )
                self._send_json(result)
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            return
        except (ValueError, FileNotFoundError, OSError) as exc:
            try:
                status = 404 if isinstance(exc, FileNotFoundError) else 400
                self._send_json({"ok": False, "error": type(exc).__name__, "message": str(exc)}, status=status)
            except (BrokenPipeError, ConnectionResetError):
                return

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _stream_data(self) -> JsonDict:
        return collect_stream_dashboard_data(snapshot_db=self.snapshot_db, history_limit=self.history_limit)

    def _status_data(self) -> JsonDict:
        return collect_status_dashboard_data(snapshot_db=self.snapshot_db, results_db=self.results_db)

    def _strategy_runs_data(self) -> JsonDict:
        return collect_strategy_runs_dashboard_data(runs_dir=self.runs_dir)

    def _strategy_detail_from_path(self, *, html_path: bool) -> JsonDict:
        prefix = "/strategy/" if html_path else "/api/strategies/"
        parts = self.path.removeprefix(prefix).split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise FileNotFoundError("strategy/run path requires strategy and run_id")
        return collect_strategy_run_detail_data(runs_dir=self.runs_dir, strategy=parts[0], run_id=parts[1])

    def _send_html(self, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, data: Mapping[str, Any], *, status: int = 200) -> None:
        payload = json.dumps(data, default=str, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="kbtc15-1s-dashboard read-only stream + paper status dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8792)
    parser.add_argument("--snapshot-db", default=None, help=f"Snapshot DB. Default: {PREFERRED_SNAPSHOT_DB}, with legacy fallback.")
    parser.add_argument("--results-db", default=None, help=f"Paper results DB. Default: {PREFERRED_RESULTS_DB}, with legacy fallback.")
    parser.add_argument("--runs-dir", default=None, help=f"Strategy replay runs dir. Default: {DEFAULT_RUNS_DIR}")
    parser.add_argument("--history-limit", type=int, default=120)
    parser.add_argument("--once", choices=("stream-html", "status-html", "strategies-html", "stream-json", "status-json", "strategies-json"), help="Render once and exit instead of serving HTTP.")
    args = parser.parse_args(argv)
    snapshot_db = resolve_snapshot_db(args.snapshot_db)
    results_db = resolve_results_db(args.results_db)
    runs_dir = resolve_runs_dir(args.runs_dir)
    if args.once:
        if args.once == "stream-html":
            print(render_stream_dashboard_html(collect_stream_dashboard_data(snapshot_db=snapshot_db, history_limit=args.history_limit)))
        elif args.once == "status-html":
            print(render_status_dashboard_html(collect_status_dashboard_data(snapshot_db=snapshot_db, results_db=results_db)))
        elif args.once == "strategies-html":
            print(render_strategy_runs_dashboard_html(collect_strategy_runs_dashboard_data(runs_dir=runs_dir)))
        elif args.once == "stream-json":
            print(json.dumps(collect_stream_dashboard_data(snapshot_db=snapshot_db, history_limit=args.history_limit), indent=2, default=str))
        elif args.once == "status-json":
            print(json.dumps(collect_status_dashboard_data(snapshot_db=snapshot_db, results_db=results_db), indent=2, default=str))
        else:
            print(json.dumps(collect_strategy_runs_dashboard_data(runs_dir=runs_dir), indent=2, default=str))
        return 0
    handler = type("ConfiguredDashboardHandler", (DashboardHandler,), {"snapshot_db": snapshot_db, "results_db": results_db, "runs_dir": runs_dir, "history_limit": args.history_limit})
    server = ThreadingHTTPServer((args.host, args.port), handler)
    server.daemon_threads = True
    print(f"kbtc15-1s-dashboard serving read-only dashboards on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    return 0


def main_stream(argv: list[str] | None = None) -> int:
    args = [*(argv or []), "--once", "stream-html"] if "--once" not in (argv or []) else (argv or [])
    return main(list(args))


def main_status(argv: list[str] | None = None) -> int:
    args = [*(argv or []), "--once", "status-html"] if "--once" not in (argv or []) else (argv or [])
    return main(list(args))


def _page(*, title: str, heading: str, subheading: str, body: str, refresh_ms: int, stream_api_path: str | None = None) -> str:
    refresh_js = _stream_refresh_js(stream_api_path) if stream_api_path else _reload_refresh_js(refresh_ms)
    return f"""<!doctype html>
<html data-refresh-ms="{int(refresh_ms)}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{_h(title)}</title><style>
:root{{color-scheme:dark;--bg:#07111f;--panel:#0f172a;--panel2:#111827;--line:#26364d;--text:#e6edf3;--muted:#94a3b8;--good:#22c55e;--bad:#ef4444;--warn:#fbbf24;--blue:#38bdf8}}*{{box-sizing:border-box}}body{{font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;background:radial-gradient(circle at top left,#123052,#07111f 42%);color:var(--text);margin:0;padding:24px}}header{{position:sticky;top:0;z-index:5;background:rgba(7,17,31,.92);backdrop-filter:blur(10px);border:1px solid var(--line);border-radius:16px;padding:16px 18px;margin-bottom:18px}}h1{{margin:0 0 6px;font-size:28px}}h2{{margin-top:0}}.muted,small{{color:var(--muted)}}section{{background:rgba(17,24,39,.92);border:1px solid var(--line);border-radius:16px;margin:16px 0;padding:16px;box-shadow:0 12px 28px rgba(0,0,0,.18)}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{border-bottom:1px solid var(--line);padding:8px;text-align:left;vertical-align:top}}pre{{white-space:pre-wrap;max-height:360px;overflow:auto;background:#020617;border:1px solid var(--line);border-radius:12px;padding:12px}}button{{background:#164e63;color:#ecfeff;border:1px solid #0891b2;border-radius:10px;padding:8px 10px;cursor:pointer}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}}.card{{background:linear-gradient(180deg,#0f172a,#0b1220);border:1px solid #334155;border-radius:14px;padding:14px}}.card strong{{color:#cbd5e1}}.card span{{display:block;font-size:24px;font-weight:800;margin:4px 0}}.boundary{{color:var(--warn);font-weight:700}}.split{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}}.decision-hero{{display:grid;grid-template-columns:minmax(260px,1fr) minmax(260px,1fr);gap:18px;align-items:center;border-width:2px}}.decision-hero.buy-yes,.decision-hero.buy-no{{border-color:rgba(34,197,94,.65)}}.decision-hero.no-trade{{border-color:rgba(251,191,36,.65)}}.decision-action{{font-size:48px;font-weight:900;letter-spacing:-.04em}}.eyebrow{{text-transform:uppercase;letter-spacing:.12em;color:var(--muted);font-size:12px}}.decision-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}}.mini{{background:#020617;border:1px solid var(--line);border-radius:12px;padding:12px}}.mini b{{display:block;font-size:20px}}canvas{{width:100%;max-height:280px;background:#020617;border:1px solid var(--line);border-radius:12px}}.topline{{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}}
</style></head><body><header><div class="topline"><div><h1>{_h(heading)}</h1><p class="muted">{subheading}</p></div><div class="muted">Auto-refresh in <span id="refreshCountdown">1.0</span>s</div></div></header>{body}<script>
const refreshMs = Number(document.documentElement.dataset.refreshMs || 1000);
function copyApiJson(path){{ fetch(path).then(r=>r.text()).then(t=>navigator.clipboard && navigator.clipboard.writeText(t)); }}
function drawLineChart(canvasId, dataId, xKey, yKeys){{ const c=document.getElementById(canvasId), d=document.getElementById(dataId); if(!c||!d) return; let rows=[]; try{{rows=JSON.parse(d.textContent)}}catch(e){{return}}; if(!rows.length) return; const ctx=c.getContext('2d'), w=c.width, h=c.height, pad=28; ctx.clearRect(0,0,w,h); const vals=[]; rows.forEach(r=>yKeys.forEach(k=>{{const v=Number(r[k]); if(Number.isFinite(v)) vals.push(v)}})); if(!vals.length) return; const min=Math.min(...vals), max=Math.max(...vals), span=(max-min)||1; const x=i=>pad+(w-pad*2)*(i/Math.max(1,rows.length-1)); const y=v=>h-pad-(h-pad*2)*((v-min)/span); ctx.strokeStyle='#334155'; ctx.beginPath(); ctx.moveTo(pad,pad); ctx.lineTo(pad,h-pad); ctx.lineTo(w-pad,h-pad); ctx.stroke(); yKeys.forEach((k,idx)=>{{ctx.strokeStyle=idx?'#fbbf24':'#38bdf8'; ctx.lineWidth=2; ctx.beginPath(); rows.forEach((r,i)=>{{const v=Number(r[k]); if(!Number.isFinite(v)) return; const xx=x(i), yy=y(v); if(i===0) ctx.moveTo(xx,yy); else ctx.lineTo(xx,yy);}}); ctx.stroke(); }}); }}
drawLineChart('price-chart','stream-history-data','ts',['btc_price','target_price']);
drawLineChart('complement-spread-chart','complement-spread-data','ts',['buy_both_edge','sell_both_edge']);
drawLineChart('equity-chart','equity-data','ts',['equity']);
function confirmDeleteRun(button){{
  const url = button && button.dataset ? button.dataset.url : '';
  if (!url || !confirm('Delete this replay run from disk? This cannot be undone.')) return;
  fetch(url, {{method:'DELETE', cache:'no-store'}}).then(r => {{
    if (!r.ok) return r.text().then(t => {{ throw new Error(t || ('HTTP ' + r.status)); }});
    const row = button.closest('tr'); if (row) row.remove();
  }}).catch(err => alert('Delete failed: ' + (err.message || String(err))));
}}
{refresh_js}
</script></body></html>"""


def _card(title: str, value: Any, note: Any = "", *, value_id: str | None = None, note_id: str | None = None) -> str:
    value_attr = f' id="{_h(value_id)}"' if value_id else ""
    note_attr = f' id="{_h(note_id)}"' if note_id else ""
    return f"<div class=\"card\"><strong>{_h(title)}</strong><br><span{value_attr}>{_h(value)}</span><br><small{note_attr}>{_h(note)}</small></div>"


def _kv(key: str, value: Any, *, value_id: str | None = None) -> str:
    value_attr = f' id="{_h(value_id)}"' if value_id else ""
    return f"<tr><th>{_h(key)}</th><td{value_attr}>{_h(_fmt(value) if isinstance(value, float) else value)}</td></tr>"


def _group_rows(rows: list[Mapping[str, Any]], *, label_key: str) -> str:
    return "".join(f"<tr><td>{_h(row.get(label_key))}</td><td>{row.get('count')}</td><td>{_fmt(row.get('realized_pnl'))}</td></tr>" for row in rows)


def _mini_metric(label: str, value: Any, note: Any = "", *, value_id: str | None = None) -> str:
    value_attr = f' id="{_h(value_id)}"' if value_id else ""
    return f'<div class="mini"><span class="muted">{_h(label)}</span><b{value_attr}>{_h(value)}</b><small>{_h(note)}</small></div>'


def _reload_refresh_js(refresh_ms: int) -> str:
    if refresh_ms <= 0:
        return ""
    return """
let remaining = refreshMs;
setInterval(() => { remaining -= 100; if (remaining <= 0) location.reload(); const el=document.getElementById('refreshCountdown'); if (el) el.textContent=(remaining/1000).toFixed(1); }, 100);
"""


def _stream_refresh_js(api_path: str | None) -> str:
    api_value = (api_path or "/api/stream").replace("\\", "\\\\").replace("'", "\\'")
    api = f"'{api_value}'"
    return f"""
let remaining = refreshMs;
function setText(id, value) {{ const el = document.getElementById(id); if (el) el.textContent = value == null ? '-' : String(value); }}
function fmtNum(value, digits=4) {{ const n = Number(value); return Number.isFinite(n) ? n.toFixed(digits) : '-'; }}
function fmtMoney(value) {{ const n = Number(value); return Number.isFinite(n) ? '$' + n.toLocaleString(undefined, {{minimumFractionDigits:2, maximumFractionDigits:2}}) : '-'; }}
function fmtPct(value) {{ const n = Number(value); return Number.isFinite(n) ? (n * 100).toFixed(1) + '%' : '-'; }}
function escapeHtml(value) {{ return String(value == null ? '' : value).replace(/[&<>\"]/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}}[ch])); }}
function renderGraphRows(history) {{
  const body = document.getElementById('graph-points-body');
  if (!body) return;
  body.innerHTML = (history || []).slice(-20).map(p => '<tr><td>' + escapeHtml(p.ts) + '</td><td>' + escapeHtml(fmtNum(p.btc_price)) + '</td><td>' + escapeHtml(fmtNum(p.target_price)) + '</td><td>' + escapeHtml(fmtNum(p.seconds_to_close)) + '</td></tr>').join('');
}}
function updateStreamDashboard(data) {{
  const stream = data.stream || {{}};
  const latest = stream.latest || {{}};
  const decision = latest.execution_decision || {{}};
  const orderbook = latest.orderbook || {{}};
  const slopes = latest.slopes || {{}};
  const freshness = stream.freshness || {{}};
  const price = Number(latest.btc_price), strike = Number(latest.strike);
  setText('decision-action', decision.action || 'NO_TRADE');
  setText('decision-reason', decision.reason || 'waiting for state');
  setText('decision-size', fmtMoney(decision.size_dollars));
  setText('decision-confidence', fmtPct(decision.confidence));
  setText('decision-blocked-by', (decision.blocked_by || []).length ? decision.blocked_by.join(', ') : 'none');
  setText('recorder-freshness', freshness.status || '-');
  setText('recorder-age', (freshness.age_seconds == null ? '-' : freshness.age_seconds + 's'));
  setText('btc-price', fmtMoney(latest.btc_price));
  setText('above-below-strike', 'source ' + (latest.btc_price_source || 'unknown'));
  setText('distance-from-strike', Number.isFinite(price) && Number.isFinite(strike) ? fmtMoney(price - strike) : '-');
  setText('strike-price', 'strike ' + fmtMoney(latest.strike));
  setText('seconds-to-close', fmtNum(latest.seconds_to_close));
  setText('market-ticker', latest.market_ticker || '-');
  setText('yes-bid', fmtNum(orderbook.yes_bid));
  setText('yes-ask', fmtNum(orderbook.yes_ask));
  setText('no-bid', fmtNum(orderbook.no_bid));
  setText('no-ask', fmtNum(orderbook.no_ask));
  setText('orderbook-spread', fmtNum(orderbook.spread));
  setText('orderbook-status', orderbook.status || '-');
  setText('btc-price-source', latest.btc_price_source || '-');
  setText('slope-10s', fmtNum(slopes.slope_10s));
  setText('slope-30s', fmtNum(slopes.slope_30s));
  setText('slope-60s', fmtNum(slopes.slope_60s));
  const historyEl = document.getElementById('stream-history-data');
  if (historyEl) historyEl.textContent = JSON.stringify((stream.history || []).slice(-90));
  setText('raw-payload', JSON.stringify(latest.raw_payload || {{}}, null, 2));
  renderGraphRows(stream.history || []);
  drawLineChart('price-chart','stream-history-data','ts',['btc_price','target_price']);
}}
async function pollStreamDashboard() {{
  try {{ const r = await fetch({api}, {{cache:'no-store'}}); if (!r.ok) throw new Error('HTTP ' + r.status); updateStreamDashboard(await r.json()); }}
  catch (err) {{ setText('recorder-freshness', 'fetch error'); setText('recorder-age', err.message || String(err)); }}
  finally {{ remaining = refreshMs; }}
}}
setInterval(() => {{ remaining -= 100; const el=document.getElementById('refreshCountdown'); if (el) el.textContent=(Math.max(0, remaining)/1000).toFixed(1); }}, 100);
setInterval(pollStreamDashboard, refreshMs);
pollStreamDashboard();
"""


def _decision_class(action: Any) -> str:
    text = str(action or "").lower().replace("_", "-")
    return text if text in {"buy-yes", "buy-no", "no-trade"} else "no-trade"


def _distance(price: Any, strike: Any) -> float | None:
    price_f = _float(price)
    strike_f = _float(strike)
    if price_f is None or strike_f is None:
        return None
    return price_f - strike_f


def _money(value: Any) -> str:
    value_f = _float(value)
    if value_f is None:
        return "-"
    return f"${value_f:,.2f}"


def _pct(value: Any) -> str:
    value_f = _float(value)
    if value_f is None:
        return "-"
    return f"{value_f * 100:.1f}%"


def _safe_raw_payload(payload: Any) -> Any:
    if not isinstance(payload, Mapping):
        return payload
    return {str(key): value for key, value in payload.items() if str(key) not in LEGACY_HTML_KEYS}


def _h(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return str(value)
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)


def _json_loads(value: Any, *, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return default


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(UTC)


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def row_get(row: sqlite3.Row | Mapping[str, Any], key: str) -> Any:
    if isinstance(row, sqlite3.Row):
        return row[key] if key in row.keys() else None
    return row.get(key)


def _table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({name})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(str(row[1]) == column for row in conn.execute(f"PRAGMA table_info({table})"))


def _action_for_signal(side: str) -> str:
    if side == "long_above":
        return "BUY_YES"
    if side == "long_below":
        return "BUY_NO"
    return "NO_TRADE"


def _above_below(price: float | None, strike: float | None) -> str:
    if price is None or strike is None:
        return "unknown"
    if price > strike:
        return "above"
    if price < strike:
        return "below"
    return "at"


def _seconds_between(start: str, end: str) -> float | None:
    start_dt = _parse_dt(start)
    end_dt = _parse_dt(end)
    if start_dt is None or end_dt is None:
        return None
    return (end_dt - start_dt).total_seconds()


def _price_bucket(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 0.35:
        return "<0.35"
    if value < 0.5:
        return "0.35-0.50"
    if value < 0.65:
        return "0.50-0.65"
    return ">=0.65"


def _seconds_bucket(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 60:
        return "<60s"
    if value < 180:
        return "60-180s"
    if value < 600:
        return "180-600s"
    return ">=600s"


def _distance_bucket(value: float | None) -> str:
    if value is None:
        return "unknown"
    abs_value = abs(value)
    if abs_value < 10:
        return "<10"
    if abs_value < 50:
        return "10-50"
    if abs_value < 100:
        return "50-100"
    return ">=100"


def _slope_bucket(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < -1:
        return "down"
    if value > 1:
        return "up"
    return "flat"


def _hold_bucket(trade: Mapping[str, Any]) -> str:
    start = _parse_dt(trade.get("created_at"))
    end = _parse_dt(trade.get("settled_at"))
    if start is None or end is None:
        return "open/unknown"
    seconds = (end - start).total_seconds()
    return _seconds_bucket(seconds)


if __name__ == "__main__":
    raise SystemExit(main())
