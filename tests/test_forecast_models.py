from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from ecommerce_agent.business.forecast_backtest import (
    ChampionSelector,
    RollingBacktest,
    compute_interval_coverage,
    compute_metrics,
    compute_pinball_loss,
)
from ecommerce_agent.business.forecast_models import (
    CrostonModel,
    EWMAForecastModel,
    RollingMeanModel,
    TSBModel,
    WeightedMovingAverageModel,
)


def test_candidate_models_are_deterministic_and_nonnegative() -> None:
    history = [Decimal(value) for value in (0, 3, 0, 6, 0, 0, 9)]
    models = [
        RollingMeanModel(window=3),
        WeightedMovingAverageModel(window=3),
        EWMAForecastModel(alpha=Decimal("0.3")),
        CrostonModel(),
        TSBModel(),
    ]

    for model in models:
        first = model.predict(history, 5)
        second = model.predict(history, 5)
        assert first == second
        assert len(first) == 5
        assert all(value >= 0 for value in first)


def test_weighted_moving_average_uses_recent_values_more_heavily() -> None:
    model = WeightedMovingAverageModel(window=3)

    assert model.predict([Decimal("1"), Decimal("2"), Decimal("10")], 2) == [
        Decimal("5.833333333333333333333333333"),
        Decimal("5.833333333333333333333333333"),
    ]


def test_rolling_backtest_never_passes_future_values_to_a_model() -> None:
    class RecordingModel:
        name = "recording"
        minimum_history_days = 3

        def __init__(self) -> None:
            self.histories: list[list[Decimal]] = []

        def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]:
            self.histories.append(list(history))
            return [history[-1]] * horizon_days

    model = RecordingModel()
    values = [Decimal(index) for index in range(1, 11)]
    dates = [date(2026, 8, 1) + timedelta(days=index) for index in range(len(values))]
    report = RollingBacktest.run(
        values,
        dates,
        models=[model],
        forecast_horizon=2,
        windows=3,
    )

    assert report.records
    assert all(max(history) < values[len(history)] for history in model.histories)
    assert all(record.forecast_start > record.training_end for record in report.records)


def test_champion_falls_back_to_baseline_without_required_improvement() -> None:
    values = [Decimal("10")] * 14
    dates = [date(2026, 8, 1) + timedelta(days=index) for index in range(len(values))]
    report = RollingBacktest.run(
        values,
        dates,
        models=[RollingMeanModel(window=3), EWMAForecastModel(alpha=Decimal("0.3"))],
        forecast_horizon=2,
        windows=3,
    )

    decision = ChampionSelector.select(
        report,
        baseline_name="rolling_mean",
        improvement_threshold=Decimal("0.05"),
    )
    assert decision.champion_model == "rolling_mean"
    assert "baseline" in decision.reason
    assert decision.ranking[0]["model"] == "rolling_mean"


def test_metrics_report_zero_denominator_without_fake_wape() -> None:
    metrics = compute_metrics([Decimal("0"), Decimal("0")], [Decimal("0"), Decimal("1")])

    assert metrics["wape"] is None
    assert metrics["bias"] is None
    assert metrics["smape"] == Decimal("1")
    assert metrics["rmse"] == Decimal("0.7071067811865475244008443621")


def test_quantile_metrics_report_pinball_and_interval_coverage() -> None:
    actual = [Decimal("1"), Decimal("3"), Decimal("2"), Decimal("4")]
    forecast = [Decimal("2"), Decimal("2"), Decimal("2"), Decimal("3")]

    assert compute_pinball_loss(actual, forecast, Decimal("0.5")) == Decimal("0.375")
    assert compute_interval_coverage(
        actual,
        lower=[Decimal("0"), Decimal("2"), Decimal("1"), Decimal("3")],
        upper=[Decimal("2"), Decimal("4"), Decimal("2"), Decimal("3")],
    ) == Decimal("0.75")


def test_champion_keeps_baseline_for_zero_demand_without_wape_denominator() -> None:
    values = [Decimal("0")] * 14
    dates = [date(2026, 1, 1) + timedelta(days=index) for index in range(14)]
    report = RollingBacktest.run(
        values,
        dates,
        models=[RollingMeanModel(window=3), EWMAForecastModel(alpha=Decimal("0.3"))],
        forecast_horizon=7,
        windows=3,
    )
    decision = ChampionSelector.select(report, baseline_name="rolling_mean")
    assert decision.champion_model == "rolling_mean"
    assert decision.reason == "baseline_fallback_all_candidates_failed"


def test_failed_candidate_does_not_block_a_usable_baseline() -> None:
    class BrokenModel:
        name = "broken"
        minimum_history_days = 3

        def predict(self, history: list[Decimal], horizon_days: int) -> list[Decimal]:
            raise ValueError("synthetic_model_failure")

    values = [Decimal("4")] * 14
    dates = [date(2026, 1, 1) + timedelta(days=index) for index in range(len(values))]
    report = RollingBacktest.run(
        values,
        dates,
        models=[RollingMeanModel(window=3), BrokenModel()],
        forecast_horizon=2,
        windows=2,
    )

    decision = ChampionSelector.select(report, baseline_name="rolling_mean")
    assert decision.champion_model == "rolling_mean"
    assert any(record.model == "broken" and record.failure_reason for record in report.records)
