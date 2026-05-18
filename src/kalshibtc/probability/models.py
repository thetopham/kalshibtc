from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

LabeledRows = Sequence[tuple[Sequence[float], bool]]


@dataclass(frozen=True)
class BrownianProbabilityModel:
    min_volatility: float = 1e-9

    def predict_proba(self, *, distance_to_strike: float, volatility: float, seconds_to_close: float) -> float:
        vol = max(abs(float(volatility)), self.min_volatility)
        time = max(float(seconds_to_close), 1.0)
        z = float(distance_to_strike) / (vol * math.sqrt(time))
        return _normal_cdf(z)


@dataclass
class LogisticProbabilityModel:
    learning_rate: float = 0.05
    iterations: int = 1000
    l2: float = 0.001
    weights: list[float] = field(default_factory=list)
    bias: float = 0.0

    def fit(self, rows: LabeledRows) -> LogisticProbabilityModel:
        if not rows:
            raise ValueError("cannot fit logistic model without rows")
        width = len(rows[0][0])
        self.weights = [0.0] * width
        self.bias = 0.0
        n = float(len(rows))
        for _ in range(max(1, int(self.iterations))):
            grad_w = [0.0] * width
            grad_b = 0.0
            for x, label in rows:
                if len(x) != width:
                    raise ValueError("all feature vectors must have same width")
                pred = self.predict_vector(x)
                err = pred - (1.0 if label else 0.0)
                grad_b += err
                for i, value in enumerate(x):
                    grad_w[i] += err * float(value)
            for i in range(width):
                grad = grad_w[i] / n + self.l2 * self.weights[i]
                self.weights[i] -= self.learning_rate * grad
            self.bias -= self.learning_rate * grad_b / n
        return self

    def predict_vector(self, features: Sequence[float]) -> float:
        if not self.weights:
            return 0.5
        score = self.bias + sum(w * float(x) for w, x in zip(self.weights, features, strict=True))
        return _sigmoid(score)


@dataclass(frozen=True)
class IsotonicCalibrator:
    thresholds: list[float]
    values: list[float]

    def predict(self, value: float) -> float:
        if not self.thresholds:
            return 0.5
        x = float(value)
        best = self.values[0]
        for threshold, calibrated in zip(self.thresholds, self.values, strict=True):
            if x < threshold:
                break
            best = calibrated
        return min(1.0, max(0.0, best))


def fit_isotonic_calibrator(probabilities: Sequence[float], labels: Sequence[bool]) -> IsotonicCalibrator:
    if len(probabilities) != len(labels):
        raise ValueError("probabilities and labels must have same length")
    pairs = sorted((float(p), 1.0 if y else 0.0) for p, y in zip(probabilities, labels, strict=True))
    if not pairs:
        return IsotonicCalibrator([], [])
    blocks: list[dict[str, float]] = []
    for prob, label in pairs:
        blocks.append({"threshold": prob, "sum": label, "weight": 1.0, "value": label})
        while len(blocks) >= 2 and blocks[-2]["value"] > blocks[-1]["value"]:
            right = blocks.pop()
            left = blocks.pop()
            merged = {
                "threshold": right["threshold"],
                "sum": left["sum"] + right["sum"],
                "weight": left["weight"] + right["weight"],
                "value": 0.0,
            }
            merged["value"] = merged["sum"] / merged["weight"]
            blocks.append(merged)
    return IsotonicCalibrator(
        thresholds=[block["threshold"] for block in blocks],
        values=[block["value"] for block in blocks],
    )


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))
