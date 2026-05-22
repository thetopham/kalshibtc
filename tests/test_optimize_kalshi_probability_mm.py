from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts import optimize_kalshi_probability_mm as opt


def test_focused_candidates_prioritize_bayesian_markov_near_positive_test_config() -> None:
    candidates = opt.build_candidates(20)

    assert candidates[0]["probability_model"] == "bayesian_markov"
    assert {cfg["probability_model"] for cfg in candidates} == {"bayesian_markov"}
    assert {cfg["edge_threshold"] for cfg in candidates} <= {0.045, 0.05, 0.055, 0.06, 0.07}
    assert {cfg["base_notional"] for cfg in candidates} <= {5.0, 7.5, 10.0}
    assert {cfg["max_net_ratio"] for cfg in candidates} <= {0.15, 0.2, 0.25, 0.3}


def test_probability_calibration_stats_reads_signal_probabilities_by_official_outcome(tmp_path: Path) -> None:
    db = tmp_path / "results.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE portfolio_settlement_by_market (market_ticker TEXT PRIMARY KEY, settlement_result TEXT)"
        )
        conn.executemany(
            "INSERT INTO portfolio_settlement_by_market (market_ticker, settlement_result) VALUES (?, ?)",
            [("m1", "yes"), ("m2", "no")],
        )
        conn.execute("CREATE TABLE replay_signals (raw_json TEXT NOT NULL)")
        rows = [
            _signal_raw("m1", 0.8, "yes"),
            _signal_raw("m1", 0.7, "none"),
            _signal_raw("m2", 0.2, "no"),
            _signal_raw("m2", 0.4, "none"),
        ]
        conn.executemany("INSERT INTO replay_signals (raw_json) VALUES (?)", [(r,) for r in rows])

    stats = opt.probability_calibration_stats(db)

    assert stats["calibration_samples"] == 4
    assert stats["brier_score"] < 0.1
    assert stats["log_loss"] < 0.5
    assert stats["ece"] < 0.3
    assert stats["bucket_count"] >= 2


def test_test_gate_rejects_overfit_and_tail_risk() -> None:
    validation = {"selection_score": 200.0, "realized_pnl": 300.0}
    weak_test = {
        "selection_score": -1.0,
        "realized_pnl": 10.0,
        "worst_market_pnl": -80.0,
        "brier_score": 0.2,
        "ece": 0.2,
    }
    robust_test = {
        "selection_score": 25.0,
        "realized_pnl": 80.0,
        "worst_market_pnl": -20.0,
        "brier_score": 0.16,
        "ece": 0.08,
    }

    rejected = opt.test_gate(weak_test, validation)
    accepted = opt.test_gate(robust_test, validation)

    assert rejected["passes_test_gate"] is False
    assert "test_selection_score_nonpositive" in rejected["test_gate_reasons"]
    assert "validation_to_test_pnl_ratio_high" in rejected["test_gate_reasons"]
    assert accepted["passes_test_gate"] is True
    assert accepted["test_gate_reasons"] == []


def _signal_raw(market_ticker: str, probability_yes: float, side: str) -> str:
    return json.dumps(
        {
            "state": {"tick_raw": {"market_ticker": market_ticker}},
            "signal": {
                "side": side,
                "features": {
                    "model_probability": probability_yes,
                    "market_probability": 1.0 - probability_yes if side == "no" else probability_yes,
                    "probability_model": "bayesian_markov",
                },
            },
        },
        sort_keys=True,
    )
