from __future__ import annotations

from decimal import Decimal
from typing import Protocol


class ForecastModel(Protocol):
    name: str
    minimum_history_days: int

    def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]: ...


class RollingMeanModel:
    name = "rolling_mean"
    minimum_history_days = 1

    def __init__(self, window: int = 7):
        if window < 1:
            raise ValueError("forecast_invalid_window")
        self.window = window

    def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]:
        if not history:
            raise ValueError("forecast_insufficient_history")
        sample = history[-self.window :]
        value = max(Decimal("0"), sum(sample, Decimal("0")) / Decimal(len(sample)))
        return [value] * horizon_days


class EWMAForecastModel:
    name = "ewma"
    minimum_history_days = 1

    def __init__(self, alpha: Decimal = Decimal("0.3")):
        if alpha <= 0 or alpha > 1:
            raise ValueError("forecast_invalid_alpha")
        self.alpha = alpha

    def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]:
        if not history:
            raise ValueError("forecast_insufficient_history")
        smooth = history[0]
        for value in history[1:]:
            smooth = self.alpha * value + (Decimal("1") - self.alpha) * smooth
        return [max(Decimal("0"), smooth)] * horizon_days


class CrostonModel:
    name = "croston"
    minimum_history_days = 3

    def __init__(self, alpha: Decimal = Decimal("0.1")):
        if alpha <= 0 or alpha > 1:
            raise ValueError("forecast_invalid_alpha")
        self.alpha = alpha

    def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]:
        if len(history) < self.minimum_history_days:
            raise ValueError("forecast_insufficient_history")
        nonzero = [index for index, value in enumerate(history) if value > 0]
        if not nonzero:
            return [Decimal("0")] * horizon_days
        demand = history[nonzero[0]]
        interval = Decimal(nonzero[0] + 1)
        last = nonzero[0]
        for index in range(nonzero[0] + 1, len(history)):
            value = history[index]
            if value <= 0:
                continue
            gap = Decimal(index - last)
            demand = self.alpha * value + (Decimal("1") - self.alpha) * demand
            interval = self.alpha * gap + (Decimal("1") - self.alpha) * interval
            last = index
        value = max(Decimal("0"), (demand / interval) * Decimal("0.95"))
        return [value] * horizon_days


class TSBModel:
    name = "tsb"
    minimum_history_days = 3

    def __init__(
        self,
        demand_alpha: Decimal = Decimal("0.2"),
        occurrence_beta: Decimal = Decimal("0.2"),
    ):
        if not (0 < demand_alpha <= 1 and 0 < occurrence_beta <= 1):
            raise ValueError("forecast_invalid_alpha")
        self.demand_alpha = demand_alpha
        self.occurrence_beta = occurrence_beta

    def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]:
        if len(history) < self.minimum_history_days:
            raise ValueError("forecast_insufficient_history")
        probability = Decimal("1") if history[0] > 0 else Decimal("0")
        demand = history[0] if history[0] > 0 else Decimal("0")
        for value in history[1:]:
            occurrence = Decimal("1") if value > 0 else Decimal("0")
            probability = (
                self.occurrence_beta * occurrence
                + (Decimal("1") - self.occurrence_beta) * probability
            )
            if value > 0:
                demand = (
                    self.demand_alpha * value
                    + (Decimal("1") - self.demand_alpha) * demand
                )
        value = max(Decimal("0"), probability * demand)
        return [value] * horizon_days
