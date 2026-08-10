from __future__ import annotations

from decimal import Decimal
from typing import Protocol


class ForecastModel(Protocol):
    name: str
    minimum_history_days: int

    def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]: ...


def _validate(history: list[Decimal], horizon_days: int, minimum: int) -> None:
    if len(history) < minimum:
        raise ValueError("forecast_insufficient_history")
    if horizon_days < 1:
        raise ValueError("forecast_invalid_horizon")


def _nonnegative(value: Decimal) -> Decimal:
    return max(Decimal("0"), value)


class LastValueModel:
    name = "last_value"
    minimum_history_days = 1

    def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]:
        _validate(history, horizon_days, self.minimum_history_days)
        return [_nonnegative(history[-1])] * horizon_days


class RollingMeanModel:
    name = "rolling_mean"
    minimum_history_days = 1

    def __init__(self, window: int = 7):
        if window < 1:
            raise ValueError("forecast_invalid_window")
        self.window = window

    def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]:
        _validate(history, horizon_days, self.minimum_history_days)
        sample = history[-self.window :]
        value = sum(sample, Decimal("0")) / Decimal(len(sample))
        return [_nonnegative(value)] * horizon_days


class WeightedMovingAverageModel:
    name = "weighted_moving_average"
    minimum_history_days = 3

    def __init__(self, window: int = 7):
        if window < 1:
            raise ValueError("forecast_invalid_window")
        self.window = window

    def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]:
        _validate(history, horizon_days, self.minimum_history_days)
        sample = history[-self.window :]
        weights = range(1, len(sample) + 1)
        denominator = Decimal(sum(weights))
        value = sum(
            (item * Decimal(weight) for item, weight in zip(sample, weights)),
            Decimal("0"),
        ) / denominator
        return [_nonnegative(value)] * horizon_days


class EWMAForecastModel:
    name = "ewma"
    minimum_history_days = 1

    def __init__(self, alpha: Decimal = Decimal("0.3")):
        if alpha <= 0 or alpha > 1:
            raise ValueError("forecast_invalid_alpha")
        self.alpha = alpha

    def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]:
        _validate(history, horizon_days, self.minimum_history_days)
        smooth = history[0]
        for value in history[1:]:
            smooth = self.alpha * value + (Decimal("1") - self.alpha) * smooth
        return [_nonnegative(smooth)] * horizon_days


class SeasonalNaiveModel:
    name = "seasonal_naive_7d"
    minimum_history_days = 14

    def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]:
        _validate(history, horizon_days, self.minimum_history_days)
        season = history[-7:]
        return [_nonnegative(season[index % 7]) for index in range(horizon_days)]


class DriftModel:
    name = "drift"
    minimum_history_days = 4

    def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]:
        _validate(history, horizon_days, self.minimum_history_days)
        slope = (history[-1] - history[0]) / Decimal(len(history) - 1)
        return [
            _nonnegative(history[-1] + slope * Decimal(index + 1))
            for index in range(horizon_days)
        ]


class SeasonalDriftModel:
    name = "seasonal_drift_7d"
    minimum_history_days = 14

    def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]:
        _validate(history, horizon_days, self.minimum_history_days)
        previous, latest = history[-14:-7], history[-7:]
        cycle_change = (
            sum(latest, Decimal("0")) - sum(previous, Decimal("0"))
        ) / Decimal(7)
        return [
            _nonnegative(latest[index % 7] + cycle_change * Decimal(index // 7 + 1))
            for index in range(horizon_days)
        ]


class CrostonModel:
    name = "croston"
    minimum_history_days = 7

    def __init__(self, alpha: Decimal = Decimal("0.1")):
        if alpha <= 0 or alpha > 1:
            raise ValueError("forecast_invalid_alpha")
        self.alpha = alpha

    def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]:
        _validate(history, horizon_days, self.minimum_history_days)
        nonzero = [index for index, value in enumerate(history) if value > 0]
        if not nonzero:
            return [Decimal("0")] * horizon_days
        size = history[nonzero[0]]
        interval = Decimal(nonzero[0] + 1)
        previous = nonzero[0]
        for index in nonzero[1:]:
            size = self.alpha * history[index] + (Decimal("1") - self.alpha) * size
            interval = (
                self.alpha * Decimal(index - previous)
                + (Decimal("1") - self.alpha) * interval
            )
            previous = index
        value = _nonnegative((size / interval) * (Decimal("1") - self.alpha / Decimal("2")))
        return [value] * horizon_days


class TSBModel:
    name = "tsb"
    minimum_history_days = 7

    def __init__(self, alpha: Decimal = Decimal("0.2"), beta: Decimal = Decimal("0.2")):
        if not (0 < alpha <= 1 and 0 < beta <= 1):
            raise ValueError("forecast_invalid_alpha")
        self.alpha = alpha
        self.beta = beta

    def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]:
        _validate(history, horizon_days, self.minimum_history_days)
        probability = Decimal("1") if history[0] > 0 else Decimal("0")
        size = history[0] if history[0] > 0 else Decimal("0")
        for value in history[1:]:
            occurrence = Decimal("1") if value > 0 else Decimal("0")
            probability = self.beta * occurrence + (Decimal("1") - self.beta) * probability
            if value > 0:
                size = self.alpha * value + (Decimal("1") - self.alpha) * size
        return [_nonnegative(probability * size)] * horizon_days


DEFAULT_FORECAST_MODELS: tuple[ForecastModel, ...] = (
    LastValueModel(),
    RollingMeanModel(),
    WeightedMovingAverageModel(),
    EWMAForecastModel(),
    SeasonalNaiveModel(),
    DriftModel(),
    SeasonalDriftModel(),
    CrostonModel(),
    TSBModel(),
)
