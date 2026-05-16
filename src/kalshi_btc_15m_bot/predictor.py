from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .config import BotConfig
from .features import FEATURE_COLUMNS, latest_features, make_supervised_dataset
from .models import KalshiMarket, ModelInfo, Prediction, now_utc
from .strategies import clamp, rule_based_probability


@dataclass(frozen=True)
class TrainResult:
    model: Pipeline | None
    info: ModelInfo


class BTC15MPredictor:
    def __init__(self, config: BotConfig) -> None:
        self.config = config

    def train(self, df: pd.DataFrame) -> TrainResult:
        X, y = make_supervised_dataset(df)
        if len(X) < self.config.predictor.model_min_train_samples or y.nunique() < 2:
            return TrainResult(
                model=None,
                info=ModelInfo(
                    name="logistic_regression_direction",
                    trained=False,
                    samples=len(X),
                    test_accuracy=None,
                    brier=None,
                    weight=0.0,
                ),
            )

        split = int(len(X) * (1.0 - self.config.predictor.model_test_fraction))
        split = max(50, min(split, len(X) - 50))
        X_train, X_test = X.iloc[:split], X.iloc[split:]
        y_train, y_test = y.iloc[:split], y.iloc[split:]
        if y_train.nunique() < 2 or y_test.nunique() < 2:
            return TrainResult(
                model=None,
                info=ModelInfo(
                    name="logistic_regression_direction",
                    trained=False,
                    samples=len(X),
                    test_accuracy=None,
                    brier=None,
                    weight=0.0,
                ),
            )

        model = Pipeline(
            [
                ("scaler", StandardScaler()),
                ("classifier", LogisticRegression(max_iter=1000, class_weight="balanced")),
            ]
        )
        model.fit(X_train, y_train)
        probabilities = model.predict_proba(X_test)[:, 1]
        predictions = (probabilities >= 0.5).astype(int)
        accuracy = float(accuracy_score(y_test, predictions))
        brier = float(brier_score_loss(y_test, probabilities))
        model_weight = clamp((accuracy - 0.50) * 4.0, 0.0, 1.0 - self.config.predictor.rule_weight_floor)

        return TrainResult(
            model=model,
            info=ModelInfo(
                name="logistic_regression_direction",
                trained=True,
                samples=len(X),
                test_accuracy=accuracy,
                brier=brier,
                weight=model_weight,
            ),
        )

    def predict(
        self,
        df: pd.DataFrame,
        *,
        market: KalshiMarket,
        current_price: float,
        now: datetime | None = None,
    ) -> Prediction:
        now = now or now_utc()
        rule = rule_based_probability(df, market=market, current_price=current_price, now=now)
        train_result = self.train(df)
        model_probability: float | None = None
        reasons = list(rule.reasons)

        if train_result.model is not None:
            latest = latest_features(df)
            latest_x = pd.DataFrame([{col: latest[col] for col in FEATURE_COLUMNS}])
            model_probability = float(train_result.model.predict_proba(latest_x)[0][1])
            reasons.append(
                "ml_probability_yes="
                f"{model_probability:.3f}; test_accuracy={train_result.info.test_accuracy:.3f}; "
                f"weight={train_result.info.weight:.2f}"
            )
        else:
            reasons.append(f"ml_skipped: only {train_result.info.samples} usable samples")

        model_weight = train_result.info.weight if model_probability is not None else 0.0
        probability_yes = (1.0 - model_weight) * rule.probability_yes + model_weight * (model_probability or 0.5)
        probability_yes = clamp(probability_yes, 0.02, 0.98)
        probability_no = 1.0 - probability_yes
        confidence = abs(probability_yes - 0.5) * 2.0

        action, side, edge, stake, gate_reason = self._decision(
            market=market,
            probability_yes=probability_yes,
            probability_no=probability_no,
            confidence=confidence,
            now=now,
        )
        reasons.extend(gate_reason)

        return Prediction(
            prediction_id=str(uuid.uuid4()),
            created_at=now,
            market=market,
            current_price=current_price,
            probability_yes=probability_yes,
            probability_no=probability_no,
            action=action,
            side=side,
            edge=edge,
            confidence=confidence,
            stake_dollars=stake,
            reasons=reasons,
            model_info=train_result.info,
            feature_snapshot=rule.feature_snapshot,
        )

    def _decision(
        self,
        *,
        market: KalshiMarket,
        probability_yes: float,
        probability_no: float,
        confidence: float,
        now: datetime,
    ) -> tuple[str, str | None, float, float, list[str]]:
        reasons: list[str] = []
        seconds = market.seconds_to_close(now)
        if seconds is None:
            return "HOLD", None, 0.0, 0.0, ["hold: market close_time missing"]
        if seconds < self.config.predictor.min_seconds_to_close:
            return "HOLD", None, 0.0, 0.0, [f"hold: too close to close ({seconds:.0f}s left)"]
        if seconds > self.config.predictor.max_seconds_to_close:
            return "HOLD", None, 0.0, 0.0, [f"hold: market not in decision window ({seconds:.0f}s left)"]
        # min_confidence is expressed as a probability threshold, e.g. 0.54.
        min_abs = max(0.0, (self.config.predictor.min_confidence - 0.5) * 2)
        if confidence < min_abs:
            return "HOLD", None, 0.0, 0.0, [f"hold: confidence {confidence:.3f} below {min_abs:.3f}"]

        yes_ask = market.yes_ask
        no_ask = market.no_ask
        yes_edge = probability_yes - yes_ask if 0.0 < yes_ask < 1.0 else -999.0
        no_edge = probability_no - no_ask if 0.0 < no_ask < 1.0 else -999.0
        reasons.append(f"edge_yes={yes_edge:.3f}; edge_no={no_edge:.3f}")

        if yes_edge >= no_edge:
            side, edge, price, probability = "YES", yes_edge, yes_ask, probability_yes
        else:
            side, edge, price, probability = "NO", no_edge, no_ask, probability_no

        if edge < self.config.predictor.min_edge:
            return "HOLD", None, edge, 0.0, reasons + [
                f"hold: best edge {edge:.3f} below min_edge {self.config.predictor.min_edge:.3f}"
            ]

        stake = self._stake(probability=probability, price=price)
        return f"BUY_{side}", side, edge, stake, reasons + [f"paper_signal: BUY_{side} stake=${stake:.2f}"]

    def _stake(self, *, probability: float, price: float) -> float:
        if not (0.0 < price < 1.0):
            return 0.0
        denominator = max(1.0 - price, 1e-6)
        kelly = max(0.0, (probability - price) / denominator)
        cap = max(self.config.paper.kelly_fraction_cap, 1e-6)
        scaled = min(1.0, kelly / cap)
        if scaled <= 0:
            return 0.0
        stake = self.config.paper.max_position_dollars * scaled
        return float(round(min(self.config.paper.max_position_dollars, max(1.0, stake)), 2))
