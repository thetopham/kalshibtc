from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

LabeledRows = Sequence[tuple[Sequence[float], bool]]


@dataclass(frozen=True)
class BrownianProbabilityModel:
    min_volatility: float = 1e-9

    def predict_proba(self, *, distance_to_strike: float, volatility: float, seconds_to_close: float) -> float:
        return probability_yes_from_distance(
            distance_to_strike=distance_to_strike,
            sigma_per_sqrt_second=volatility,
            seconds_to_expiry=seconds_to_close,
            min_sigma=self.min_volatility,
        )


@dataclass(frozen=True)
class DistanceProbability:
    distance_to_strike: float
    sigma_per_sqrt_second: float
    seconds_to_expiry: float
    z_score: float
    probability_yes: float
    probability_no: float


def probability_yes_from_z(z_score: float) -> float:
    """Risk-neutral-ish Brownian baseline P(BTC expiry > strike | z).

    z = (current_price - strike) / (sigma * sqrt(seconds_to_expiry)).
    Under a driftless normal/Brownian terminal move assumption, YES probability
    is Phi(z); NO probability is 1 - Phi(z). This is a model input, not a truth
    claim: calibrate it against settled 15m markets before using it as edge.
    """

    return _normal_cdf(float(z_score))


def probability_yes_from_distance(
    *,
    distance_to_strike: float,
    sigma_per_sqrt_second: float,
    seconds_to_expiry: float,
    min_sigma: float = 1e-9,
) -> float:
    vol = max(abs(float(sigma_per_sqrt_second)), min_sigma)
    time = max(float(seconds_to_expiry), 1.0)
    z = float(distance_to_strike) / (vol * math.sqrt(time))
    return probability_yes_from_z(z)


def distance_probability(
    *,
    distance_to_strike: float,
    sigma_per_sqrt_second: float,
    seconds_to_expiry: float,
    min_sigma: float = 1e-9,
) -> DistanceProbability:
    vol = max(abs(float(sigma_per_sqrt_second)), min_sigma)
    time = max(float(seconds_to_expiry), 1.0)
    z = float(distance_to_strike) / (vol * math.sqrt(time))
    yes = probability_yes_from_z(z)
    return DistanceProbability(
        distance_to_strike=float(distance_to_strike),
        sigma_per_sqrt_second=vol,
        seconds_to_expiry=time,
        z_score=z,
        probability_yes=yes,
        probability_no=1.0 - yes,
    )



@dataclass(frozen=True)
class MarkovTrendPrediction:
    probability_yes: float
    model_probability_yes: float
    market_prior_yes: float | None
    confidence: float
    regime_probabilities: dict[str, float]


@dataclass(frozen=True)
class SyntheticMarkovConfig:
    start_price: float = 100_000.0
    seconds: int = 900
    transition_matrix: Mapping[str, Mapping[str, float]] = field(
        default_factory=lambda: _default_transition_matrix()
    )
    drift_by_state: Mapping[str, float] = field(
        default_factory=lambda: {"up": 1.25, "down": -1.25, "chop": 0.0}
    )
    volatility_by_state: Mapping[str, float] = field(
        default_factory=lambda: {"up": 2.5, "down": 2.5, "chop": 7.0}
    )
    initial_state: str = "chop"
    seed: int | None = None


@dataclass(frozen=True)
class SyntheticMarkovPath:
    prices: list[float]
    states: list[str]
    returns: list[float]


@dataclass(frozen=True)
class SyntheticMarkovPathGenerator:
    config: SyntheticMarkovConfig = field(default_factory=SyntheticMarkovConfig)

    def generate(self) -> SyntheticMarkovPath:
        rng = random.Random(self.config.seed)
        states = list(self.config.transition_matrix)
        state = self.config.initial_state if self.config.initial_state in states else states[0]
        prices = [float(self.config.start_price)]
        hidden_states: list[str] = []
        returns: list[float] = []
        for _ in range(max(0, int(self.config.seconds))):
            hidden_states.append(state)
            ret = rng.gauss(
                float(self.config.drift_by_state.get(state, 0.0)),
                max(float(self.config.volatility_by_state.get(state, 1.0)), 1e-9),
            )
            returns.append(ret)
            prices.append(max(0.01, prices[-1] + ret))
            state = _sample_transition(rng, self.config.transition_matrix[state])
        return SyntheticMarkovPath(prices=prices, states=hidden_states, returns=returns)


@dataclass(frozen=True)
class BayesianMarkovTrendModel:
    transition_matrix: Mapping[str, Mapping[str, float]] = field(
        default_factory=lambda: _default_transition_matrix()
    )
    drift_by_state: Mapping[str, float] = field(
        default_factory=lambda: {"up": 1.25, "down": -1.25, "chop": 0.0}
    )
    volatility_by_state: Mapping[str, float] = field(
        default_factory=lambda: {"up": 2.5, "down": 2.5, "chop": 7.0}
    )
    monte_carlo_paths: int = 500
    random_seed: int = 1
    min_volatility: float = 1e-9

    def predict_from_returns(
        self,
        *,
        recent_returns: Sequence[float],
        current_price: float,
        strike: float,
        seconds_to_close: float,
        market_prior_yes: float | None = None,
    ) -> MarkovTrendPrediction:
        posterior = self.regime_posterior(recent_returns)
        model_probability = self._simulate_probability_yes(
            posterior=posterior,
            current_price=float(current_price),
            strike=float(strike),
            seconds_to_close=float(seconds_to_close),
        )
        confidence = max(posterior.values()) - 1.0 / max(len(posterior), 1)
        confidence = max(0.0, min(1.0, confidence))
        if market_prior_yes is None:
            blended = model_probability
        else:
            prior = max(0.0, min(1.0, float(market_prior_yes)))
            weight = 0.25 + 0.70 * confidence
            blended = prior * (1.0 - weight) + model_probability * weight
        return MarkovTrendPrediction(
            probability_yes=max(0.0, min(1.0, blended)),
            model_probability_yes=model_probability,
            market_prior_yes=market_prior_yes,
            confidence=confidence,
            regime_probabilities=dict(posterior),
        )

    def regime_posterior(self, recent_returns: Sequence[float]) -> dict[str, float]:
        states = list(self.transition_matrix)
        if not states:
            return {}
        probabilities = {state: 1.0 / len(states) for state in states}
        for observed_return in recent_returns:
            predicted = {
                to_state: sum(
                    probabilities[from_state]
                    * float(self.transition_matrix.get(from_state, {}).get(to_state, 0.0))
                    for from_state in states
                )
                for to_state in states
            }
            likelihood_weighted = {
                state: predicted[state]
                * _normal_pdf(
                    float(observed_return),
                    mean=float(self.drift_by_state.get(state, 0.0)),
                    sigma=max(float(self.volatility_by_state.get(state, 1.0)), self.min_volatility),
                )
                for state in states
            }
            probabilities = _normalize(likelihood_weighted)
        return probabilities

    def _simulate_probability_yes(
        self,
        *,
        posterior: Mapping[str, float],
        current_price: float,
        strike: float,
        seconds_to_close: float,
    ) -> float:
        mean_terminal = 0.0
        variance_terminal = 0.0
        for state, probability in posterior.items():
            p = max(0.0, float(probability))
            drift = float(self.drift_by_state.get(state, 0.0))
            volatility = max(float(self.volatility_by_state.get(state, 1.0)), self.min_volatility)
            horizon = max(1.0, float(seconds_to_close))
            terminal_mean_for_state = drift * horizon
            terminal_variance_for_state = volatility * volatility * horizon
            mean_terminal += p * terminal_mean_for_state
            variance_terminal += p * (terminal_variance_for_state + terminal_mean_for_state * terminal_mean_for_state)
        variance_terminal = max(self.min_volatility, variance_terminal - mean_terminal * mean_terminal)
        z = (float(current_price) + mean_terminal - float(strike)) / math.sqrt(variance_terminal)
        return probability_yes_from_z(z)

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


def _default_transition_matrix() -> dict[str, dict[str, float]]:
    return {
        "up": {"up": 0.94, "down": 0.02, "chop": 0.04},
        "down": {"up": 0.02, "down": 0.94, "chop": 0.04},
        "chop": {"up": 0.05, "down": 0.05, "chop": 0.90},
    }


def _sample_transition(rng: random.Random, weights: Mapping[str, float]) -> str:
    if not weights:
        raise ValueError("cannot sample from empty transition weights")
    total = sum(max(0.0, float(weight)) for weight in weights.values())
    if total <= 0:
        return next(iter(weights))
    draw = rng.random() * total
    cumulative = 0.0
    last = next(iter(weights))
    for state, weight in weights.items():
        last = state
        cumulative += max(0.0, float(weight))
        if draw <= cumulative:
            return state
    return last


def _normalize(weights: Mapping[str, float]) -> dict[str, float]:
    total = sum(max(0.0, float(value)) for value in weights.values())
    if total <= 0:
        if not weights:
            return {}
        uniform = 1.0 / len(weights)
        return {key: uniform for key in weights}
    return {key: max(0.0, float(value)) / total for key, value in weights.items()}


def _normal_pdf(value: float, *, mean: float, sigma: float) -> float:
    sigma = max(abs(float(sigma)), 1e-9)
    z = (float(value) - float(mean)) / sigma
    return math.exp(-0.5 * z * z) / (sigma * math.sqrt(2.0 * math.pi))
