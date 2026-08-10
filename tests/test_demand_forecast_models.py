from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from ecommerce_agent.business.forecast_evaluation import (
    classify_demand,
    rolling_backtest,
)
from ecommerce_agent.business.forecast_models import (
    CrostonModel,
    DriftModel,
    EWMAForecastModel,
    LastValueModel,
    RollingMeanModel,
    SeasonalDriftModel,
    SeasonalNaiveModel,
    TSBModel,
    WeightedMovingAverageModel,
)


def _decimals(values: list[int]) -> list[Decimal]:
    return [Decimal(value) for value in values]


def test_candidate_models_are_deterministic_nonnegative_and_horizon_safe() -> None:
    history = _decimals([0, 3, 0, 6, 0, 0, 9, 0, 4, 0, 8, 0, 0, 10])
    models = [
        LastValueModel(),
        RollingMeanModel(),
        WeightedMovingAverageModel(),
        EWMAForecastModel(),
        SeasonalNaiveModel(),
        DriftModel(),
        SeasonalDriftModel(),
        CrostonModel(),
        TSBModel(),
    ]

    for model in models:
        first = model.predict(history, 7)
        assert first == model.predict(history, 7)
        assert len(first) == 7
        assert all(value >= 0 for value in first)


def test_trend_and_trend_seasonal_models_extend_observed_shape() -> None:
    assert DriftModel().predict(_decimals([2, 4, 6, 8]), 3) == _decimals([10, 12, 14])

    history = _decimals([3, 5, 8, 6, 9, 12, 7, 5, 7, 10, 8, 11, 14, 9])
    assert SeasonalDriftModel().predict(history, 7) == _decimals(
        [7, 9, 12, 10, 13, 16, 11]
    )


def test_rolling_backtest_never_passes_holdout_values_to_model() -> None:
    class RecordingModel:
        name = "recording"
        minimum_history_days = 4

        def __init__(self) -> None:
            self.histories: list[list[Decimal]] = []

        def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]:
            self.histories.append(list(history))
            return [history[-1]] * horizon_days

    model = RecordingModel()
    values = _decimals(list(range(1, 29)))
    dates = [date(2026, 1, 1) + timedelta(days=index) for index in range(len(values))]
    report = rolling_backtest(values, dates, [model], horizon_days=7, windows=3)

    assert len(report.records) == 3
    assert all(record.training_end < record.forecast_start for record in report.records)
    assert all(max(history) < values[len(history)] for history in model.histories)


def test_demand_shape_is_separate_from_data_quality_flags() -> None:
    weekly = _decimals([4, 6, 8, 10, 12, 14, 16] * 4)
    profile = classify_demand(weekly, quality_flags=("missing_dates",))

    assert profile.kind == "weekly_seasonal"
    assert profile.quality_flags == ("missing_dates",)
    assert classify_demand(_decimals([0, 0, 7, 0, 0, 0, 5] * 4)).kind == "intermittent"
    assert classify_demand(
        _decimals([0, 0, 3, 0, 0, 18, 0, 0, 0, 7, 0, 25, 0, 0] * 2)
    ).kind == "lumpy"
