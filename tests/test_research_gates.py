from __future__ import annotations

from kalshibtc.research.gates import ResearchGateThresholds, evaluate_research_gates


def test_research_gates_pass_when_metrics_clear_thresholds() -> None:
    metrics = {
        "trades": 25,
        "total_pnl": 140.0,
        "max_drawdown": 35.0,
        "sharpe": 1.4,
    }
    thresholds = ResearchGateThresholds(
        min_trades=20,
        require_positive_pnl=True,
        max_drawdown=50.0,
        min_sharpe=1.0,
        min_sharpe_trades=10,
    )

    result = evaluate_research_gates(metrics, thresholds)

    assert result["passed"] is True
    assert result["failed_reasons"] == []
    assert result["thresholds"] == {
        "min_trades": 20,
        "require_positive_pnl": True,
        "max_drawdown": 50.0,
        "min_sharpe": 1.0,
        "min_sharpe_trades": 10,
    }
    assert result["metrics"] == {
        "trades": 25,
        "total_pnl": 140.0,
        "max_drawdown": 35.0,
        "sharpe": 1.4,
    }


def test_research_gates_report_all_failures_with_serializable_thresholds() -> None:
    metrics = {
        "trades": 8,
        "total_pnl": -3.25,
        "max_drawdown": 120.0,
        "sharpe": -0.2,
    }
    thresholds = ResearchGateThresholds(
        min_trades=20,
        require_positive_pnl=True,
        max_drawdown=50.0,
        min_sharpe=1.0,
        min_sharpe_trades=5,
    )

    result = evaluate_research_gates(metrics, thresholds)

    assert result["passed"] is False
    assert result["failed_reasons"] == [
        "trades 8 below minimum 20",
        "total_pnl -3.25 is not positive",
        "max_drawdown 120.0 exceeds cap 50.0",
        "sharpe -0.2 below minimum 1.0",
    ]
    assert result["thresholds"]["min_trades"] == 20


def test_research_gates_skip_sharpe_until_sample_size_is_sufficient() -> None:
    metrics = {
        "trades": 4,
        "total_pnl": 10.0,
        "max_drawdown": 5.0,
        "sharpe": -99.0,
    }
    thresholds = ResearchGateThresholds(
        min_trades=1,
        require_positive_pnl=True,
        max_drawdown=10.0,
        min_sharpe=1.0,
        min_sharpe_trades=10,
    )

    result = evaluate_research_gates(metrics, thresholds)

    assert result["passed"] is True
    assert result["failed_reasons"] == []


def test_research_gates_missing_metrics_fail_safe_without_exceptions() -> None:
    result = evaluate_research_gates({}, ResearchGateThresholds(min_trades=1))

    assert result["passed"] is False
    assert "trades 0 below minimum 1" in result["failed_reasons"]
    assert "total_pnl 0.0 is not positive" in result["failed_reasons"]
