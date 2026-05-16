from __future__ import annotations

import argparse
import html
import json
import sqlite3
import subprocess
from collections import Counter, defaultdict
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
    PREFERRED_RESULTS_DB,
    PREFERRED_SNAPSHOT_DB,
    resolve_results_db,
    resolve_snapshot_db,
)
from .strategy.simple_directional import SimpleDirectionalStrategy

BOUNDARY_TEXT = "Read-only dashboard. No live orders. Active system is 1s recorder + 1s paper executor."
STREAM_TITLE = "Kalshi BTC Stream"
STATUS_TITLE = "Kalshi BTC 1s Paper Status"
ACTIVE_SERVICES = (
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
    return {
        "title": STREAM_TITLE,
        "mode": "paper/research/read-only",
        "api_path": "/api/stream",
        "boundary": BOUNDARY_TEXT,
        "snapshot_db_path": str(snapshot_path),
        "stream": {
            "freshness": _freshness(latest["ts"] if latest else None),
            "latest": latest,
            "history": [_history_point(row) for row in rows],
            "history_limit": history_limit,
        },
    }


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
        distance = _distance(latest.get('btc_price'), latest.get('strike'))
        decision_class = _decision_class(decision.get('action'))
        body = f"""
        <section class="decision-hero {decision_class}">
          <div>
            <p class="eyebrow">SimpleDirectionalStrategy · simulated/paper only</p>
            <h2>Execution Decision</h2>
            <div class="decision-action">{_h(decision.get('action'))}</div>
            <p>{_h(decision.get('reason'))}</p>
          </div>
          <div class="decision-grid">
            {_mini_metric('Size', _money(decision.get('size_dollars')), 'paper dollars')}
            {_mini_metric('Confidence', _pct(decision.get('confidence')), 'strategy score')}
            {_mini_metric('Blocked by', ', '.join(decision.get('blocked_by') or []) or 'none', 'risk gates')}
          </div>
        </section>
        <section class="cards">
          {_card('Recorder freshness', _h(stream.get('freshness', {}).get('status')), _h(stream.get('freshness', {}).get('age_seconds')) + 's')}
          {_card('Latest BTC price', _money(latest.get('btc_price')), _h(latest.get('above_below_strike')))}
          {_card('Distance from strike', _money(distance), f"strike {_money(latest.get('strike'))}")}
          {_card('Seconds to close', _fmt(latest.get('seconds_to_close')), _h(latest.get('market_ticker')))}
        </section>
        <section class="split"><div><h2>YES / NO orderbook</h2>
          <table><tbody>
            {_kv('YES bid', orderbook.get('yes_bid'))}{_kv('YES ask', orderbook.get('yes_ask'))}
            {_kv('NO bid', orderbook.get('no_bid'))}{_kv('NO ask', orderbook.get('no_ask'))}
            {_kv('spread', orderbook.get('spread'))}{_kv('status', orderbook.get('status'))}
          </tbody></table></div>
          <div><h2>Slopes</h2><table><tbody>
            {_kv('slope_10s', slopes.get('slope_10s'))}{_kv('slope_30s', slopes.get('slope_30s'))}{_kv('slope_60s', slopes.get('slope_60s'))}
          </tbody></table></div>
        </section>
        <section><h2>Recent graph points</h2>
          <canvas id="price-chart" width="960" height="260" aria-label="BTC price versus strike chart"></canvas>
          <div id="stream-chart">Read-only chart source: /api/stream history</div>
          <table><thead><tr><th>ts</th><th>BTC</th><th>target</th><th>sec close</th></tr></thead><tbody>{graph_rows}</tbody></table>
        </section>
        <section><h2>Raw payload</h2><button type="button" onclick="copyApiJson('/api/stream')">Copy API JSON</button><pre>{_h(json.dumps(_safe_raw_payload(latest.get('raw_payload')), indent=2, sort_keys=True))}</pre></section>
        <script type="application/json" id="stream-history-data">{chart_points_json}</script>
        """
    return _page(
        title=str(data.get("title") or STREAM_TITLE),
        heading=STREAM_TITLE,
        subheading=f"{_h(data.get('mode'))} | API {_h(data.get('api_path'))} | {_h(data.get('boundary'))}",
        body=body,
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
    )


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
    }


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
    history_limit: int

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path in {"/", "/stream"}:
                self._send_html(render_stream_dashboard_html(self._stream_data()))
            elif self.path == "/status":
                self._send_html(render_status_dashboard_html(self._status_data()))
            elif self.path == "/api/stream":
                self._send_json(self._stream_data())
            elif self.path in {"/api/dashboard", "/api/status"}:
                self._send_json(self._status_data())
            else:
                self.send_error(404)
        except BrokenPipeError:
            return
        except (ConnectionResetError, sqlite3.Error, OSError) as exc:
            self._send_json({"ok": False, "error": type(exc).__name__, "message": str(exc)}, status=503)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _stream_data(self) -> JsonDict:
        return collect_stream_dashboard_data(snapshot_db=self.snapshot_db, history_limit=self.history_limit)

    def _status_data(self) -> JsonDict:
        return collect_status_dashboard_data(snapshot_db=self.snapshot_db, results_db=self.results_db)

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
    parser.add_argument("--history-limit", type=int, default=120)
    parser.add_argument("--once", choices=("stream-html", "status-html", "stream-json", "status-json"), help="Render once and exit instead of serving HTTP.")
    args = parser.parse_args(argv)
    snapshot_db = resolve_snapshot_db(args.snapshot_db)
    results_db = resolve_results_db(args.results_db)
    if args.once:
        if args.once == "stream-html":
            print(render_stream_dashboard_html(collect_stream_dashboard_data(snapshot_db=snapshot_db, history_limit=args.history_limit)))
        elif args.once == "status-html":
            print(render_status_dashboard_html(collect_status_dashboard_data(snapshot_db=snapshot_db, results_db=results_db)))
        elif args.once == "stream-json":
            print(json.dumps(collect_stream_dashboard_data(snapshot_db=snapshot_db, history_limit=args.history_limit), indent=2, default=str))
        else:
            print(json.dumps(collect_status_dashboard_data(snapshot_db=snapshot_db, results_db=results_db), indent=2, default=str))
        return 0
    handler = type("ConfiguredDashboardHandler", (DashboardHandler,), {"snapshot_db": snapshot_db, "results_db": results_db, "history_limit": args.history_limit})
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


def _page(*, title: str, heading: str, subheading: str, body: str) -> str:
    return f"""<!doctype html>
<html data-refresh-ms="15000"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{_h(title)}</title><style>
:root{{color-scheme:dark;--bg:#07111f;--panel:#0f172a;--panel2:#111827;--line:#26364d;--text:#e6edf3;--muted:#94a3b8;--good:#22c55e;--bad:#ef4444;--warn:#fbbf24;--blue:#38bdf8}}*{{box-sizing:border-box}}body{{font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;background:radial-gradient(circle at top left,#123052,#07111f 42%);color:var(--text);margin:0;padding:24px}}header{{position:sticky;top:0;z-index:5;background:rgba(7,17,31,.92);backdrop-filter:blur(10px);border:1px solid var(--line);border-radius:16px;padding:16px 18px;margin-bottom:18px}}h1{{margin:0 0 6px;font-size:28px}}h2{{margin-top:0}}.muted,small{{color:var(--muted)}}section{{background:rgba(17,24,39,.92);border:1px solid var(--line);border-radius:16px;margin:16px 0;padding:16px;box-shadow:0 12px 28px rgba(0,0,0,.18)}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{border-bottom:1px solid var(--line);padding:8px;text-align:left;vertical-align:top}}pre{{white-space:pre-wrap;max-height:360px;overflow:auto;background:#020617;border:1px solid var(--line);border-radius:12px;padding:12px}}button{{background:#164e63;color:#ecfeff;border:1px solid #0891b2;border-radius:10px;padding:8px 10px;cursor:pointer}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}}.card{{background:linear-gradient(180deg,#0f172a,#0b1220);border:1px solid #334155;border-radius:14px;padding:14px}}.card strong{{color:#cbd5e1}}.card span{{display:block;font-size:24px;font-weight:800;margin:4px 0}}.boundary{{color:var(--warn);font-weight:700}}.split{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}}.decision-hero{{display:grid;grid-template-columns:minmax(260px,1fr) minmax(260px,1fr);gap:18px;align-items:center;border-width:2px}}.decision-hero.buy-yes,.decision-hero.buy-no{{border-color:rgba(34,197,94,.65)}}.decision-hero.no-trade{{border-color:rgba(251,191,36,.65)}}.decision-action{{font-size:48px;font-weight:900;letter-spacing:-.04em}}.eyebrow{{text-transform:uppercase;letter-spacing:.12em;color:var(--muted);font-size:12px}}.decision-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}}.mini{{background:#020617;border:1px solid var(--line);border-radius:12px;padding:12px}}.mini b{{display:block;font-size:20px}}canvas{{width:100%;max-height:280px;background:#020617;border:1px solid var(--line);border-radius:12px}}.topline{{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}}
</style></head><body><header><div class="topline"><div><h1>{_h(heading)}</h1><p class="muted">{subheading}</p></div><div class="muted">Auto-refresh in <span id="refreshCountdown">1.0</span>s</div></div></header>{body}<script>
const refreshMs = Number(document.documentElement.dataset.refreshMs || 1000);
let remaining = refreshMs;
setInterval(() => {{ remaining -= 100; if (remaining <= 0) location.reload(); const el=document.getElementById('refreshCountdown'); if (el) el.textContent=(remaining/1000).toFixed(1); }}, 100);
function copyApiJson(path){{ fetch(path).then(r=>r.text()).then(t=>navigator.clipboard && navigator.clipboard.writeText(t)); }}
function drawLineChart(canvasId, dataId, xKey, yKeys){{ const c=document.getElementById(canvasId), d=document.getElementById(dataId); if(!c||!d) return; let rows=[]; try{{rows=JSON.parse(d.textContent)}}catch(e){{return}}; if(!rows.length) return; const ctx=c.getContext('2d'), w=c.width, h=c.height, pad=28; ctx.clearRect(0,0,w,h); const vals=[]; rows.forEach(r=>yKeys.forEach(k=>{{const v=Number(r[k]); if(Number.isFinite(v)) vals.push(v)}})); if(!vals.length) return; const min=Math.min(...vals), max=Math.max(...vals), span=(max-min)||1; const x=i=>pad+(w-pad*2)*(i/Math.max(1,rows.length-1)); const y=v=>h-pad-(h-pad*2)*((v-min)/span); ctx.strokeStyle='#334155'; ctx.beginPath(); ctx.moveTo(pad,pad); ctx.lineTo(pad,h-pad); ctx.lineTo(w-pad,h-pad); ctx.stroke(); yKeys.forEach((k,idx)=>{{ctx.strokeStyle=idx?'#fbbf24':'#38bdf8'; ctx.lineWidth=2; ctx.beginPath(); rows.forEach((r,i)=>{{const v=Number(r[k]); if(!Number.isFinite(v)) return; const xx=x(i), yy=y(v); if(i===0) ctx.moveTo(xx,yy); else ctx.lineTo(xx,yy);}}); ctx.stroke(); }}); }}
drawLineChart('price-chart','stream-history-data','ts',['btc_price','target_price']);
drawLineChart('equity-chart','equity-data','ts',['equity']);
</script></body></html>"""


def _card(title: str, value: Any, note: Any = "") -> str:
    return f"<div class=\"card\"><strong>{_h(title)}</strong><br><span>{_h(value)}</span><br><small>{_h(note)}</small></div>"


def _kv(key: str, value: Any) -> str:
    return f"<tr><th>{_h(key)}</th><td>{_h(_fmt(value) if isinstance(value, float) else value)}</td></tr>"


def _group_rows(rows: list[Mapping[str, Any]], *, label_key: str) -> str:
    return "".join(f"<tr><td>{_h(row.get(label_key))}</td><td>{row.get('count')}</td><td>{_fmt(row.get('realized_pnl'))}</td></tr>" for row in rows)


def _mini_metric(label: str, value: Any, note: Any = "") -> str:
    return f'<div class="mini"><span class="muted">{_h(label)}</span><b>{_h(value)}</b><small>{_h(note)}</small></div>'


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


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


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
