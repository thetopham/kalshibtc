# AI Quant Research Loop PRD / Implementation Plan

> For Hermes: implement this with strict TDD. Keep all work replay/research-only until the user explicitly approves a live-order boundary change.

Goal: Build an offline AI-assisted quant research loop that proposes Kalshi BTC strategy candidates, replays them against the recorded 1s feed, scores them with institutional metrics, rejects weak/overfit candidates, and reports the best next experiment.

Architecture: The canonical market tape remains `feed/kalshi-btc-1s.sqlite3`. Candidate strategies write immutable artifacts under `runs/` and research notes under `research/`. The first milestone avoids arbitrary LLM Python by using deterministic strategy specs/parameter sweeps; AI code generation is a later, quarantined phase.

Tech Stack: Python 3.11, pytest, local SQLite, existing `kalshibtc.replay` CLI, Hermes cron for scheduled research reports.

Safety Boundary:
- Read-only feed DB access only.
- No broker/Kalshi order submission.
- No automatic changes to active systemd services.
- No automatic promotion of generated strategies into the active registry.
- Cron may create research specs, run directories, and markdown reports only.

---

## Product Requirements

### R1. Institutional Replay Metrics

Every replay run must produce metrics that are useful for comparing strategies, not just counts.

Required metrics:
- `trades`
- `wins`
- `losses`
- `win_rate`
- `total_pnl`
- `ev_per_trade`
- `max_drawdown`
- `max_drawdown_pct`
- `sharpe`
- `sortino`
- `calmar`
- `volatility`
- `mean_return`
- `skewness`
- `excess_kurtosis`
- `profit_factor`
- `avg_win`
- `avg_loss`
- `largest_win`
- `largest_loss`
- `gross_profit`
- `gross_loss`
- `notional`

Acceptance criteria:
- Metrics work with simple fill-like dicts and dataclass fills.
- Empty/low-sample runs return safe zero values, not exceptions.
- Replay `metrics.json` includes an `institutional_metrics` object.
- Existing dashboard/run tests still pass.

### R2. Validation Gates

Add deterministic gate evaluation after metrics exist.

Initial gates:
- minimum trades
- positive total PnL
- max drawdown cap
- minimum Sharpe, only if sample size is sufficient
- maximum absolute correlation to accepted candidates, later
- deflated Sharpe / multiple-testing penalty, later

Acceptance criteria:
- Gate output includes `passed`, `failed_reasons`, and threshold values.
- Failed candidates are still saved with reasons.

### R3. Candidate Strategy Specs

Represent AI-generated ideas as JSON specs before code.

Spec fields:
- `name`
- `economic_story`
- `mechanism`
- `parameters`
- `falsification`
- `created_at`
- `parent_id`

Acceptance criteria:
- Specs validate without importing arbitrary code.
- Specs can instantiate a deterministic parameterized strategy.
- Invalid specs fail with clear reasons.

### R4. Research Runner CLI

Create a local command that runs candidate specs through replay.

Proposed command:
`kbtc-research run --spec research/specs/example.json --feed-db feed/kalshi-btc-1s.sqlite3 --runs-dir runs/research --json`

Acceptance criteria:
- Writes a run directory per candidate.
- Writes `research_summary.json` with metrics and gate results.
- Never mutates feed DB or active strategy registry.

### R5. Hermes Cron Research Report

After manual runner is stable, schedule a Hermes cron job.

Behavior:
- Generate a bounded number of candidate specs.
- Run replay over selected windows.
- Rank candidates by gates and metrics.
- Write `research/reports/YYYY-MM-DD.md`.
- Deliver a short report with one next move and an explicit no-live-action boundary.

Acceptance criteria:
- Cron prompt is self-contained.
- Has a hard cap on candidates/LLM calls.
- Silent or concise when no meaningful candidate improves baseline.

---

## Implementation Tasks

### Task 1: Add institutional metric tests

Objective: Lock the desired metric semantics before implementation.

Files:
- Modify: `tests/test_replay_research_risk_controls.py`
- Modify: `src/kalshibtc/backtest/metrics.py`

Steps:
1. Add a failing test for `compute_metrics()` with deterministic pnl/notional fills.
2. Assert Sharpe/Sortino/Calmar/profit factor/drawdown fields exist and are sensible.
3. Run the specific test and confirm RED.
4. Implement minimal metric calculations.
5. Run the focused test and full relevant replay tests.

### Task 2: Add metrics.json institutional payload

Objective: Replays persist institutional metrics for dashboards and research reports.

Files:
- Modify: `tests/test_replay_research_risk_controls.py`
- Modify: `src/kalshibtc/replay/cli.py`

Steps:
1. Add a failing replay CLI test asserting `metrics.json["institutional_metrics"]` exists.
2. Wire `_metrics_payload()` to call `compute_metrics(report.fills)`.
3. Preserve existing top-level count fields for dashboard compatibility.
4. Run focused replay tests.

### Task 3: Dashboard comparison columns

Objective: Surface institutional metrics in the read-only strategy comparison page.

Files:
- Modify: `tests/test_strategy_replay_dashboard.py`
- Modify: `src/kalshibtc/dashboard.py`

Steps:
1. Add tests for Sharpe, max drawdown, profit factor table landmarks.
2. Update dashboard data collection to pass nested metrics through.
3. Render compact metric columns.
4. Verify no `location.reload`, no live-order language.

### Task 4: Validation gate module

Objective: Reject weak research candidates deterministically.

Files:
- Create: `src/kalshibtc/research/gates.py`
- Create: `tests/test_research_gates.py`

Steps:
1. Test pass/fail output for min trades, positive PnL, max drawdown, and Sharpe.
2. Implement `evaluate_research_gates(metrics, thresholds)`.
3. Keep thresholds explicit and serializable.

### Task 5: Candidate spec schema

Objective: Make AI output safe and reviewable before code.

Files:
- Create: `src/kalshibtc/research/specs.py`
- Create: `tests/test_research_specs.py`

Steps:
1. Test valid/invalid JSON candidate specs.
2. Implement dataclass or Pydantic-free validation to avoid new dependency.
3. Add canonical serialization with sorted keys.

### Task 6: Parameterized strategy adapter

Objective: Run candidate specs without arbitrary generated Python.

Files:
- Create: `src/kalshibtc/strategy/parameterized_late_window.py`
- Modify: `src/kalshibtc/strategy/registry.py` only if manually approved.
- Create: `tests/test_parameterized_strategy.py`

Steps:
1. Test spec parameters map to strategy behavior.
2. Implement strategy using existing `MarketState`/`Signal` seams.
3. Keep it out of the active registry until research runner can instantiate it directly.

### Task 7: Research runner CLI

Objective: Run one candidate spec through replay and write research artifacts.

Files:
- Create: `src/kalshibtc/research/runner.py`
- Modify: `pyproject.toml` to add `kbtc-research` script.
- Create: `tests/test_research_runner.py`

Steps:
1. Test runner writes `research_summary.json` and run outputs under a temp dir.
2. Implement bounded single-spec runner.
3. Verify feed DB is opened read-only.

### Task 8: Hermes cron prompt

Objective: Schedule the bounded research loop only after CLI is stable.

Files:
- Create: `research/prompts/nightly_ai_quant_research.md`

Steps:
1. Write self-contained cron prompt with hard candidate/LLM caps.
2. Include safety boundary and output format.
3. Manually run once before creating recurring cron.

---

## First Milestone Definition of Done

- Institutional metrics are implemented and tested.
- `kbtc-replay` writes nested `institutional_metrics` in `metrics.json`.
- Existing tests pass.
- No active services or live-order code paths are changed.
